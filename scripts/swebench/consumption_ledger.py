#!/usr/bin/env python3
"""GT block consumption ledger.

Two schemas share this module:

* ``gt.consumption_ledger.v2`` (mini-swe-agent trajectories) — the live product
  surface. A trajectory is ``{"messages": [{"role": ..., "content": ...}], ...}``.
  A GT delivery is a model-visible message (``user`` = the step-0 brief bundle,
  ``tool`` = per-view/per-edit runtime evidence) whose content carries a
  ``<gt-*>`` block. Each delivery gets a monotone RECEIPT along the ladder:
    - level 1 (delivered): the GT bytes are present in a model-visible message.
    - level 2 (referenced): a LATER **assistant-role** message's prose text
      (model-authored only, never tool output) names an entity from the block.
    - level 3 (acted): a LATER assistant message's emitted shell command targets
      a file/symbol named in the block (view/edit/test of the delivered target).
    - level 4 (resolved) is out of scope for v2 -> ``resolved_state = None``.
  ``consumed`` == receipt >= 3 (ACTION, not token overlap). When a runtime ledger
  is supplied, each delivered ledger row is joined to its trajectory block
  (content match primary: file + byte-length; iteration alignment secondary).
  Unjoined ledger rows are surfaced (never dropped) as ``source="ledger_only"``.

* ``gt.consumption_ledger.v1`` (legacy pier/OH step lists) — a list of step dicts
  with ``observation``/``action`` keys. Preserved verbatim for back-compat.

The reference defect (run 29217805592) was a reader/writer schema mismatch: the
v1 reader looked for ``data["trajectory"]``/``["steps"]`` and returned
``gt_blocks_delivered: 0`` on a mini trajectory carrying 15 visible GT blocks.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from typing import Any

# --------------------------------------------------------------------------- #
# Shared constants
# --------------------------------------------------------------------------- #
_TAG_KIND: dict[str, str] = {
    "gt-evidence": "l3b.evidence",
    "gt-contract": "l3b.contract",
    "gt-scope": "consensus.scope",
    "gt-cochange": "cochange",
    "gt-nudge": "nudge",
    "gt-task-brief": "brief.task",
    "gt-localization": "brief.localization",
    "gt-obligations": "brief.obligations",
}

# Any GT block, opening tag -> matching close tag (non-greedy body).
_BLOCK_RE = re.compile(r"<(gt-[a-z0-9_-]+)\b[^>]*>.*?</\1>", re.S | re.I)
_FILE_ATTR_RE = re.compile(r'file="([^"]+)"')
_SRC_EXT = r"(?:py|go|rs|js|ts|tsx|jsx|java|rb|c|cc|cpp|h|hpp|cs|kt|scala|php|swift)"
_PATH_RE = re.compile(r"([\w./-]+\.%s)\b" % _SRC_EXT)

# Symbol-noise stoplist (lowercased). Common Python/type tokens that would
# false-positive as "referenced" entities. Kept small and conservative.
_SYMBOL_STOP = {
    "self", "none", "true", "false", "pass", "type", "dict", "list", "tuple",
    "value", "tags", "span", "bool", "impl", "test", "tests", "return",
    "returns", "optional", "iterator", "callable", "object", "class", "async",
    "await", "yield", "raise", "print", "super", "property", "staticmethod",
    "classmethod", "isinstance", "getattr", "setattr", "hasattr",
}

# Byte-length tolerance for the content-based ledger join. The host counts the
# block plus (usually) one framing newline, so blocks measure ~1 char shorter
# than ``chars_delivered``; 16 comfortably absorbs framing/whitespace drift.
_JOIN_TOL = 16


def _content_hash16(text: str) -> str:
    """sha256 hex[:16] of the exact bytes (v2 entries carry this)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# v2 — mini-swe-agent messages[]
# --------------------------------------------------------------------------- #
def _as_mini_messages(trajectory: list[dict] | dict) -> list[dict] | None:
    """Return the mini-swe-agent ``messages`` list, or None if not mini-shape."""
    if isinstance(trajectory, dict):
        msgs = trajectory.get("messages")
        if isinstance(msgs, list) and any(
            isinstance(m, dict) and "role" in m for m in msgs
        ):
            return [m for m in msgs if isinstance(m, dict)]
        return None
    if isinstance(trajectory, list):
        # A list of role/content dicts is a mini message list; a list of
        # observation/action dicts is a legacy step list.
        if trajectory and all(isinstance(m, dict) for m in trajectory):
            roled = sum(1 for m in trajectory if "role" in m)
            if roled and roled >= len(trajectory) // 2:
                return list(trajectory)
        return None
    return None


def _emitted_commands(msg: dict) -> str:
    """Model-authored shell command text emitted by an assistant message.

    Prefers ``extra.actions[*].command`` (mini canonical), falls back to
    ``tool_calls[*].function.arguments`` (OpenAI tool-call JSON). This is the
    level-3 (acted) signal only — never used for level-2 references.
    """
    parts: list[str] = []
    extra = msg.get("extra")
    if isinstance(extra, dict):
        for act in extra.get("actions") or []:
            if isinstance(act, dict) and isinstance(act.get("command"), str):
                parts.append(act["command"])
    if not parts:
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    obj = json.loads(args)
                    cmd = obj.get("command") if isinstance(obj, dict) else None
                    if isinstance(cmd, str):
                        parts.append(cmd)
                        continue
                except ValueError:
                    pass
                parts.append(args)
    fc = msg.get("function_call")
    if isinstance(fc, dict) and isinstance(fc.get("arguments"), str):
        parts.append(fc["arguments"])
    return "\n".join(parts)


def _assistant_prose(msg: dict) -> str:
    """Model-authored natural-language text of an assistant message (level-2 only)."""
    c = msg.get("content")
    return c if isinstance(c, str) else ""


def _block_entities(block: str, file_attr: str | None) -> tuple[set[str], set[str]]:
    """(file_tokens, symbols) delivered by a GT block.

    Files: the ``file="..."`` attr plus any source-path tokens in the body, each
    as both full path and basename. Symbols: extracted only from structured
    evidence/contract markers (never free-text) to keep false-references low.
    """
    files: set[str] = set()
    if file_attr:
        files.add(file_attr)
        files.add(os.path.basename(file_attr))
    for f in _PATH_RE.findall(block):
        files.add(f)
        files.add(os.path.basename(f))

    symbols: set[str] = set()
    for m in re.findall(r"\bdef\s+([A-Za-z_]\w+)\s*\(", block):
        symbols.add(m)
    for m in re.findall(r"\bclass\s+([A-Za-z_]\w+)", block):
        symbols.add(m)
    for m in re.findall(r"\[CALLERS\]\s+([A-Za-z_]\w+)", block):
        symbols.add(m)
    for m in re.findall(r"\[CONSUMED\]\s+callers of\s+([A-Za-z_]\w+)", block):
        symbols.add(m)
    for m in re.findall(r"\[WITNESS\]\s+([A-Za-z_]\w+)\s+(?:called by|calls)", block):
        symbols.add(m)
    for line in re.findall(r"\[SIBLINGS\]\s*([^\n]+)", block):
        for part in line.split(","):
            mm = re.match(r"\s*([A-Za-z_]\w+)", part)
            if mm:
                symbols.add(mm.group(1))
    symbols = {s for s in symbols if len(s) >= 4 and s.lower() not in _SYMBOL_STOP}
    # A file basename is already covered by ``files``; don't double it as a symbol.
    symbols -= files
    return files, symbols


def _entity_patterns(files: set[str], symbols: set[str]) -> list[re.Pattern[str]]:
    """Word-boundary matchers for every delivered entity."""
    pats: list[re.Pattern[str]] = []
    for ent in files | symbols:
        if ent:
            pats.append(re.compile(r"\b" + re.escape(ent) + r"\b"))
    return pats


def _named_in(text: str, pats: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in pats)


def _build_v2(
    messages: list[dict],
    *,
    runtime_ledger_path: str | None = None,
) -> dict[str, Any]:
    n = len(messages)

    # Pre-compute per-message model-authored channels once.
    assistant_prose: list[str] = [""] * n
    assistant_cmd: list[str] = [""] * n
    tool_ordinal: list[int | None] = [None] * n
    ord_counter = 0
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            assistant_prose[i] = _assistant_prose(m)
            assistant_cmd[i] = _emitted_commands(m)
        elif role == "tool":
            ord_counter += 1
            tool_ordinal[i] = ord_counter

    # 1) Extract every model-visible GT block as a delivery.
    entries: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role not in ("user", "tool"):
            continue  # assistant = model-authored, system/exit = framing
        content = m.get("content")
        if not isinstance(content, str) or "<gt-" not in content:
            continue
        channel = "brief" if role == "user" else "runtime"
        for mm in _BLOCK_RE.finditer(content):
            block = mm.group(0)
            tag = mm.group(1).lower()
            kind = _TAG_KIND.get(tag, tag)
            fa = _FILE_ATTR_RE.search(block)
            file_attr = fa.group(1) if fa else None
            files, symbols = _block_entities(block, file_attr)
            pats = _entity_patterns(files, symbols)

            # 2) Receipt ladder over LATER messages (strict msg_index > i).
            receipt = 1
            ref_idx: int | None = None
            act_idx: int | None = None
            verif = False
            for j in range(i + 1, n):
                if messages[j].get("role") != "assistant":
                    continue
                if ref_idx is None and pats and _named_in(assistant_prose[j], pats):
                    ref_idx = j
                    receipt = max(receipt, 2)
                cmd = assistant_cmd[j]
                if cmd:
                    if act_idx is None and pats and _named_in(cmd, pats):
                        act_idx = j
                        receipt = max(receipt, 3)
                    if not verif and re.search(r"\b(pytest|unittest|py\.test|tox|nox)\b|python -m pytest", cmd):
                        verif = True

            entries.append({
                "msg_index": i,
                "tool_ordinal": tool_ordinal[i],
                "kind": kind,
                "delivery_channel": channel,
                "file_path": file_attr,
                "chars": len(block),
                "content_sha256_16": _content_hash16(block),
                "receipt": receipt,
                "referenced_msg_index": ref_idx,
                "acted_msg_index": act_idx,
                "resolved_state": None,  # level 4 out of scope for v2
                "verification_followup": verif,
                "joined": None,
                "ledger_layer": None,
                "ledger_event_type": None,
                "ledger_chars": None,
                "join_method": None,
                "source": "trajectory",
            })

    # 3) Join delivered runtime-ledger rows to trajectory blocks.
    join_rate: float | None = None
    ledger_rows_delivered = 0
    ledger_rows_joined = 0
    if runtime_ledger_path and os.path.isfile(runtime_ledger_path):
        rows = _load_delivered_ledger_rows(runtime_ledger_path)
        ledger_rows_delivered = len(rows)
        used: set[int] = set()

        def _basename_match(bf: str | None, rf: str | None) -> bool:
            if not bf or not rf:
                return False
            return os.path.basename(bf) == os.path.basename(rf)

        for row in rows:
            rf = row.get("file_path") or ""
            chars = int(row.get("chars_delivered") or 0)
            it = row.get("iteration")
            chosen: int | None = None
            method = None
            # primary: file basename + byte-length within tolerance,
            # tie-broken by iteration/ordinal proximity.
            cands = [
                k for k, e in enumerate(entries)
                if k not in used
                and e["source"] == "trajectory"
                and _basename_match(e["file_path"], rf)
                and abs(e["chars"] - chars) <= _JOIN_TOL
            ]
            if cands:
                def _prox(k: int) -> tuple[int, int]:
                    e = entries[k]
                    o = e["tool_ordinal"]
                    d = abs(o - it) if (o is not None and isinstance(it, int)) else 10**6
                    return (d, abs(e["chars"] - chars))
                chosen = min(cands, key=_prox)
                method = "content"
            elif isinstance(it, int):
                # secondary: the block sitting in the it-th tool message.
                icands = [
                    k for k, e in enumerate(entries)
                    if k not in used
                    and e["source"] == "trajectory"
                    and e["tool_ordinal"] == it
                ]
                if icands:
                    chosen = icands[0]
                    method = "iteration"
            if chosen is not None:
                used.add(chosen)
                ledger_rows_joined += 1
                e = entries[chosen]
                e["joined"] = True
                e["join_method"] = method
                e["ledger_layer"] = row.get("layer")
                e["ledger_event_type"] = row.get("event_type")
                e["ledger_chars"] = chars
            else:
                # host-only delivery not visible as a block — never drop it.
                entries.append({
                    "msg_index": None,
                    "tool_ordinal": it if isinstance(it, int) else None,
                    "kind": str(row.get("layer") or "unknown"),
                    "delivery_channel": "runtime",
                    "file_path": rf or None,
                    "chars": chars,
                    "content_sha256_16": None,
                    "receipt": None,  # not model-visible as a block; cannot verify
                    "referenced_msg_index": None,
                    "acted_msg_index": None,
                    "resolved_state": None,
                    "verification_followup": False,
                    "joined": False,
                    "ledger_layer": row.get("layer"),
                    "ledger_event_type": row.get("event_type"),
                    "ledger_chars": chars,
                    "join_method": None,
                    "source": "ledger_only",
                })
        # trajectory blocks with no ledger match keep joined=False (host silent).
        for e in entries:
            if e["source"] == "trajectory" and e["joined"] is None:
                e["joined"] = False
        join_rate = (
            round(ledger_rows_joined / ledger_rows_delivered, 8)
            if ledger_rows_delivered
            else None
        )

    # 4) Aggregate.
    visible = [e for e in entries if e["source"] == "trajectory"]
    delivered = len(visible)
    consumed = sum(1 for e in visible if (e["receipt"] or 0) >= 3)
    referenced = sum(1 for e in visible if (e["receipt"] or 0) >= 2)
    verification_followup = sum(1 for e in visible if e["verification_followup"])

    per_class: dict[str, dict[str, int]] = {}
    for e in visible:
        pc = per_class.setdefault(
            e["kind"], {"delivered": 0, "referenced": 0, "acted": 0, "max_level": 0}
        )
        r = e["receipt"] or 0
        pc["delivered"] += 1
        if r >= 2:
            pc["referenced"] += 1
        if r >= 3:
            pc["acted"] += 1
        pc["max_level"] = max(pc["max_level"], r)

    return {
        "schema": "gt.consumption_ledger.v2",
        # v1-compatible top-level keys (existing callers depend on these) --------
        "gt_blocks_delivered": delivered,
        "gt_blocks_consumed": consumed,  # consumption == ACTION (receipt>=3)
        "gt_blocks_verification_followup": verification_followup,
        "gt_blocks_hard_enforced": 0,
        "gt_blocks_enforced": 0,
        "enforcement_semantics": "receipt_ladder",
        # v2 additions -----------------------------------------------------------
        "gt_blocks_referenced": referenced,
        "receipt_ladder": {"1": "delivered", "2": "referenced", "3": "acted", "4": "resolved (out of scope v2)"},
        "receipt_distribution": _receipt_distribution(visible),
        "per_class": per_class,
        "join_rate": join_rate,
        "ledger_rows_delivered": ledger_rows_delivered,
        "ledger_rows_joined": ledger_rows_joined,
        "runtime_ledger_path": runtime_ledger_path,
        "entries": entries,
    }


def _receipt_distribution(visible: list[dict[str, Any]]) -> dict[str, int]:
    dist = {"1": 0, "2": 0, "3": 0}
    for e in visible:
        r = e["receipt"] or 0
        if 1 <= r <= 3:
            dist[str(r)] += 1
    return dist


def _load_delivered_ledger_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("outcome") == "delivered"
                    and int(row.get("chars_delivered") or 0) > 0
                ):
                    rows.append(row)
    except OSError:
        return []
    return rows


# --------------------------------------------------------------------------- #
# v1 — legacy pier/OH step list (preserved verbatim)
# --------------------------------------------------------------------------- #
_V1_BLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("l3b.evidence", r"<gt-evidence"),
    ("l3b.contract", r"<gt-contract"),
    ("brief", r"<gt-task-brief"),
    ("consensus", r"<gt-scope"),
    ("cochange", r"<gt-cochange"),
    ("nudge", r"<gt-nudge"),
    ("graph_map", r"\[WITNESS\]|\[CALLER\]|\[IMPACT\]"),
)
_V1_FILE_RE = re.compile(r"(?:^|\s)([\w./-]+\.(?:py|go|rs|js|ts|tsx|jsx|java|rb))\b")


def _v1_extract_blocks(observation: str) -> list[dict[str, str]]:
    obs = observation or ""
    blocks: list[dict[str, str]] = []
    seen_kinds: set[str] = set()
    content_hash = hashlib.sha256(obs[:2000].encode("utf-8", errors="replace")).hexdigest()[:16]
    for kind, pat in _V1_BLOCK_PATTERNS:
        if kind in seen_kinds:
            continue
        if re.search(pat, obs, re.I):
            blocks.append({"kind": kind, "content_hash": content_hash})
            seen_kinds.add(kind)
    return blocks


def _v1_action_files(action: str) -> set[str]:
    return {m.group(1) for m in _V1_FILE_RE.finditer(action or "")}


def _v1_token_overlap(a: str, b: str) -> bool:
    tokens_a = {t.lower() for t in re.findall(r"[A-Za-z_][\w]{4,}", a or "")}
    tokens_b = {t.lower() for t in re.findall(r"[A-Za-z_][\w]{4,}", b or "")}
    return bool(tokens_a & tokens_b)


def _build_v1_legacy(trajectory: list[dict] | dict, *, window: int = 3) -> dict[str, Any]:
    """Legacy step-list consumption ledger (schema v1) — unchanged behavior."""
    if isinstance(trajectory, dict):
        steps = trajectory.get("trajectory") or trajectory.get("steps") or []
    else:
        steps = trajectory
    if not isinstance(steps, list):
        steps = []

    entries: list[dict[str, Any]] = []
    delivered = consumed = verification_followup = hard_enforced = 0

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        obs = str(step.get("observation") or step.get("output") or "")
        blocks = _v1_extract_blocks(obs)
        if not blocks:
            continue
        turn = i + 1
        future_actions: list[str] = []
        for j in range(i + 1, min(i + 1 + window, len(steps))):
            act = str((steps[j] or {}).get("action") or "")
            if act:
                future_actions.append(act)
        future_text = "\n".join(future_actions)
        future_files: set[str] = set()
        for act in future_actions:
            future_files |= _v1_action_files(act)

        for blk in blocks:
            delivered += 1
            is_consumed = _v1_token_overlap(obs, future_text) or bool(future_files)
            has_verification_followup = any(
                "pytest" in a.lower() or "test" in a.lower() for a in future_actions
            )
            is_hard_enforced = False
            if is_consumed:
                consumed += 1
            if has_verification_followup:
                verification_followup += 1
            entries.append({
                "turn": turn,
                "kind": blk["kind"],
                "content_hash": blk["content_hash"],
                "consumed": is_consumed,
                "verification_followup": has_verification_followup,
                "hard_enforced": is_hard_enforced,
                "enforced": is_hard_enforced,
                "window": window,
            })

    return {
        "schema": "gt.consumption_ledger.v1",
        "gt_blocks_delivered": delivered,
        "gt_blocks_consumed": consumed,
        "gt_blocks_verification_followup": verification_followup,
        "gt_blocks_hard_enforced": hard_enforced,
        "gt_blocks_enforced": hard_enforced,
        "enforcement_semantics": "hard_block_only",
        "legacy_note": "pre-CP semantics counted test-looking follow-up as enforced",
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# Public API (signatures preserved)
# --------------------------------------------------------------------------- #
def build_consumption_ledger(
    trajectory: list[dict] | dict,
    *,
    window: int = 3,
    runtime_ledger_path: str | None = None,
) -> dict[str, Any]:
    """Delivered -> referenced -> acted receipts from a trajectory.

    Dispatches on shape: mini-swe-agent ``messages[]`` -> v2 receipt ladder;
    legacy pier/OH step list -> v1 (unchanged). ``runtime_ledger_path`` is used
    only on the v2 path (join delivered ledger rows to visible blocks).
    """
    messages = _as_mini_messages(trajectory)
    if messages is not None:
        return _build_v2(messages, runtime_ledger_path=runtime_ledger_path)
    return _build_v1_legacy(trajectory, window=window)


def _glob_runtime_ledger(traj_path: str) -> str | None:
    """Find a sibling ``gt_runtime_ledger*.jsonl`` next to the trajectory."""
    d = os.path.dirname(os.path.abspath(traj_path))
    matches = sorted(glob.glob(os.path.join(d, "gt_runtime_ledger*.jsonl")))
    return matches[0] if matches else None


def ledger_from_trajectory_path(
    path: str,
    *,
    window: int = 3,
    runtime_ledger_path: str | None = None,
) -> dict[str, Any]:
    """Load a trajectory JSON and build its consumption ledger.

    Auto-globs a sibling ``gt_runtime_ledger*.jsonl`` for the v2 join when
    ``runtime_ledger_path`` is not supplied.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return build_consumption_ledger([], window=window)

    if runtime_ledger_path is None:
        runtime_ledger_path = _glob_runtime_ledger(path)

    messages = _as_mini_messages(data)
    if messages is not None:
        return _build_v2(messages, runtime_ledger_path=runtime_ledger_path)

    # legacy path: data is a dict with "trajectory"/"steps" or a bare list
    steps = data.get("trajectory") if isinstance(data, dict) else data
    return build_consumption_ledger(steps or [], window=window)
