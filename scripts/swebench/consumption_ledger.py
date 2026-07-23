#!/usr/bin/env python3
"""GT block consumption ledger.

Two schemas share this module:

* ``gt.consumption_ledger.v2`` (mini-swe-agent trajectories) — the live product
  surface. A trajectory is ``{"messages": [{"role": ..., "content": ...}], ...}``.
  A GT delivery is exact sealed bytes in a model-visible message (``user`` = the
  step-0 brief bundle, ``tool`` = per-view/per-edit runtime evidence). Native
  Profile-2 deliveries are tag-free and are located by
  ``content_sha256_16`` + ``chars_delivered``. Legacy ``<gt-*>`` blocks remain
  readable for backward compatibility. Each delivery gets a monotone RECEIPT:
    - level 1 (delivered): the GT bytes are present in a model-visible message.
    - level 2 (referenced): a LATER **assistant-role** message's prose text
      (model-authored only, never tool output) names an entity from the block.
    - level 3 (acted): a LATER assistant message emits a relevant mutating or
      verification command targeting a file/symbol named in the block. Passive
      view/read/search commands are reacquisition, never implementation.
    - level 4 (resolved) is out of scope for v2 -> ``resolved_state = None``.
  ``consumed`` == receipt >= 3 (ACTION, not token overlap). When a runtime ledger
  is supplied, a sealed row joins only when its exact rendered bytes occur in
  the trajectory. Unsealed legacy rows retain the historical file/length join.
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
from typing import Any, Iterable

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

# Level-3 requires a decision/action that can implement or test the delivered
# constraint. Merely naming the target in cat/sed -n/grep is reacquisition and
# cannot prove consumption. These patterns classify command intent; entity
# relevance is checked separately against exact delivered file/symbol tokens.
_MUTATING_COMMAND_RE = re.compile(
    r"(?:^|[;&|\s])(?:"
    r"apply_patch|patch(?:\s|$)|git\s+apply|"
    r"sed\s+-i(?:\S*)?|perl\s+-p?i(?:\S*)?|"
    r"tee(?:\s|$)|truncate(?:\s|$)|touch(?:\s|$)|"
    r"rm(?:\s|$)|mv(?:\s|$)|cp(?:\s|$)|"
    r"str_replace|create_file|write_file|overwrite|insert|"
    r"black(?:\s|$)|ruff\s+format|prettier(?:\s|$)|go\s+fmt"
    r")|(?:^|[;&|\s])(?:cat|printf|echo)\b[^\n]*?(?<![<>])>{1,2}(?!>)",
    re.I,
)
_PYTHON_MUTATING_COMMAND_RE = re.compile(
    r"(?:^|[;&|\s])(?:python(?:3(?:\.\d+)*)?|py)\b[\s\S]*?(?:"
    r"\.(?:write|writelines|write_text|write_bytes)\s*\(|"
    r"\bopen\s*\(\s*[^,\n]+,\s*[\"'][^\"']*[wax+][^\"']*[\"']|"
    r"\bopen\s*\([^)]*\bmode\s*=\s*[\"'][^\"']*[wax+][^\"']*[\"']|"
    r"\.open\s*\(\s*[\"'][^\"']*[wax+][^\"']*[\"']"
    r")",
    re.I,
)
_VERIFICATION_COMMAND_RE = re.compile(
    r"(?:^|[;&|\s])(?:"
    r"pytest|py\.test|unittest|tox|nox|vitest|jest|"
    r"cargo\s+(?:test|check)|go\s+(?:test|vet)|"
    r"npm\s+(?:test|run\s+(?:test|build|lint))|"
    r"yarn\s+(?:test|run\s+(?:test|build|lint))|"
    r"pnpm\s+(?:test|run\s+(?:test|build|lint))|"
    r"python\s+-m\s+(?:pytest|unittest|py_compile|compileall)|"
    r"mypy|pyright|ruff\s+check|flake8|pylint|tsc|"
    r"mvn\s+test|gradle\s+test|make\s+test"
    r")\b",
    re.I,
)


def _content_hash16(text: str) -> str:
    """sha256 hex[:16] of the exact bytes (v2 entries carry this)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _locate_seal_spans(
    content: str, n_chars: int, sha16: str,
) -> list[tuple[int, int]]:
    """Locate exact sealed windows as half-open *character* spans.

    The seam records character length and hashes the UTF-8 rendered bytes. Try
    the ASCII-equivalent byte window first, then the authoritative character
    window for non-ASCII payloads. A byte-sized match is converted back to
    character offsets before it is returned; callers must never slice a Python
    string with UTF-8 byte offsets.
    """
    if not content or n_chars <= 0 or not sha16:
        return []
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    raw = content.encode("utf-8")
    if n_chars <= len(raw):
        for start in range(0, len(raw) - n_chars + 1):
            raw_window = raw[start:start + n_chars]
            if hashlib.sha256(raw_window).hexdigest()[:16] != sha16:
                continue
            try:
                char_start = len(raw[:start].decode("utf-8"))
                char_end = char_start + len(raw_window.decode("utf-8"))
            except UnicodeDecodeError:
                continue
            span = (char_start, char_end)
            if span not in seen:
                spans.append(span)
                seen.add(span)
    if n_chars <= len(content):
        for start in range(0, len(content) - n_chars + 1):
            window = content[start:start + n_chars]
            if hashlib.sha256(window.encode("utf-8")).hexdigest()[:16] == sha16:
                span = (start, start + n_chars)
                if span not in seen:
                    spans.append(span)
                    seen.add(span)
    return spans


def _locate_seal(content: str, n_chars: int, sha16: str) -> int | None:
    """Backward-compatible first-match offset for internal/contract callers."""
    spans = _locate_seal_spans(content, n_chars, sha16)
    return spans[0][0] if spans else None


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """Return whether non-empty half-open character spans share visible bytes."""
    return left[0] < right[1] and right[0] < left[1]


def _span_contains(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    """Return whether ``outer`` fully accounts for ``inner``."""
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _physical_id(msg_index: int, start: int, end: int) -> str:
    """Stable identity for one physical model-visible character interval."""
    return f"m{msg_index}:{start}:{end}"


def _typed_lineage_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the additive v1 lineage columns from one sealed ledger row.

    This transports producer claims; it does not convert receipt into causality
    or infer absent features from a layer, flag, or rendered text.
    """
    if row.get("lineage_schema") != "gt.feature_lineage.v1":
        return None
    text_keys = (
        "runtime_producer_id", "registered_producer_id", "evidence_type",
        "fact_class", "required_event", "actual_event", "receipt_predicate",
        "causal_eval", "causal_probe_id",
    )
    if any(not isinstance(row.get(key), str) for key in text_keys):
        return None
    refs = row.get("feature_ids")
    if not isinstance(refs, list) or not refs:
        return None
    try:
        from groundtruth.runtime.feature_lineage import (
            FeatureRef,
            build_lineage,
            lineage_to_dict,
        )
    except Exception:
        return None
    normalized: list[dict[str, str]] = []
    typed_refs = []
    for ref in refs:
        if not isinstance(ref, dict):
            return None
        category, feature_id, role = (
            ref.get("category"), ref.get("feature_id"), ref.get("role"))
        # v1 producers create FACT and explicit byte-owning CAP rows only.
        # ACQ/PERF are reserved schema categories, not inferred here.
        if category not in {"CAP", "FACT"}:
            return None
        if not isinstance(feature_id, str) or not feature_id:
            return None
        if not isinstance(role, str) or not role:
            return None
        try:
            typed_refs.append(FeatureRef(category, feature_id, role))
        except ValueError:
            return None
        normalized.append({
            "category": category, "feature_id": feature_id, "role": role,
        })
    if tuple(typed_refs) != tuple(sorted(set(typed_refs))):
        return None
    fact_refs = [
        ref for ref in normalized
        if ref["category"] == "FACT" and ref["role"] == "fact"
    ]
    if fact_refs != [{
        "category": "FACT", "feature_id": row["fact_class"], "role": "fact",
    }]:
        return None
    producer_match = row.get("producer_registration_match")
    causal_proven = row.get("causal_contribution_proven")
    reactive = row.get("reactive")
    if not all(isinstance(value, bool) for value in (
            producer_match, causal_proven, reactive)):
        return None
    cap_ids = tuple(
        ref["feature_id"] for ref in normalized if ref["category"] == "CAP"
    )
    try:
        rebuilt = build_lineage(
            runtime_producer_id=row["runtime_producer_id"],
            evidence_type=row["evidence_type"],
            actual_event=row["actual_event"],
            cap_feature_ids=cap_ids,
        )
    except ValueError:
        return None
    if rebuilt is None:
        return None
    expected = lineage_to_dict(rebuilt)
    observed = {
        "schema": row["lineage_schema"],
        **{key: row[key] for key in text_keys},
        "producer_registration_match": producer_match,
        "features": normalized,
        "causal_contribution_proven": causal_proven,
        "reactive": reactive,
    }
    # Delivery-ledger lineage is accepted only when it exactly matches current
    # registry/owner authorities.  Receipt never self-promotes causal credit.
    if not expected["producer_registration_match"] or observed != expected:
        return None
    return expected


# --------------------------------------------------------------------------- #
# Test-identity leak scanner (SS-3 defect-4)
#
# GT surfaces must NEVER carry a FAIL_TO_PASS/PASS_TO_PASS spec or a test
# identifier — that is answer leakage and disqualifies any consumption claim.
# The prior scanners substring-matched short test-name tokens, so generic prose
# ("no passing test between edits", "/testbed/…", "apply the patch") tripped a
# false LEAK on the words "test"/"patch"/"testbed". A leak requires a STRUCTURAL
# test identifier, matched with a minimum-length guard:
#   * a ``::``-qualified node id whose leaf is ``test_…``   (path::…::test_name)
#   * a bare pytest function name ``\btest_\w{3,}\b``        (≥3 chars after test_)
#   * a literal FAIL_TO_PASS / PASS_TO_PASS spec marker
# "test", "tests", "testbed", "patch" carry no ``test_``-prefixed word and never
# flag; a real F2P id ("tests/x.py::test_foo") always does.
# --------------------------------------------------------------------------- #
_F2P_MARKER_RE = re.compile(r"\b(?:FAIL_TO_PASS|PASS_TO_PASS)\b")
#: a ``::``-qualified id whose final segment is a pytest test (path/class::…::test_x)
_QUALIFIED_TEST_RE = re.compile(r"\S+::(?:[A-Za-z_]\w*::)*test_\w+")
#: a bare pytest function name — word boundary + ``test_`` + >=3 word chars. The
#: length guard is what keeps "test"/"testbed" (no ``test_`` word) out.
_BARE_TEST_RE = re.compile(r"(?<![\w.])test_\w{3,}\b")


def _test_id_leaf(test_id: str) -> str | None:
    """The pytest function-name leaf of a ``::``-qualified id, if it is a real
    ``test_…`` name meeting the min-length guard; else None."""
    leaf = test_id.rsplit("::", 1)[-1].strip()
    # drop a parametrize suffix: test_foo[case-1] -> test_foo
    leaf = re.sub(r"\[.*$", "", leaf)
    return leaf if re.fullmatch(r"test_\w{3,}", leaf) else None


def scan_test_identity_leaks(
    text: str, known_test_ids: Iterable[str] | None = None
) -> list[str]:
    """Return structural test-identity leaks found in ``text`` (empty = clean).

    Matches only full ``::``-qualified ids, bare ``test_\\w{3,}`` function names on a
    word boundary, and literal FAIL_TO_PASS/PASS_TO_PASS markers — NEVER a naked
    substring of a short token. When ``known_test_ids`` is supplied (the task's
    FAIL_TO_PASS/PASS_TO_PASS set), each is matched by its FULL identifier and by its
    ``test_…`` leaf on a word boundary — again, never a bare substring.
    """
    if not text:
        return []
    hits: set[str] = set()
    for m in _F2P_MARKER_RE.finditer(text):
        hits.add(m.group(0))
    for m in _QUALIFIED_TEST_RE.finditer(text):
        hits.add(m.group(0))
    for m in _BARE_TEST_RE.finditer(text):
        hits.add(m.group(0))
    for tid in known_test_ids or ():
        tid = (tid or "").strip()
        if not tid:
            continue
        # full-identifier match (the id is specific — this is not a naked token).
        if "::" in tid and tid in text:
            hits.add(tid)
        leaf = _test_id_leaf(tid) if "::" in tid else (
            tid if re.fullmatch(r"test_\w{3,}", tid) else None
        )
        if leaf and re.search(r"(?<![\w.])" + re.escape(leaf) + r"\b", text):
            hits.add(leaf)
    return sorted(hits)


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


def _emitted_commands(msg: dict) -> list[str]:
    """Model-authored actions emitted by one assistant message.

    Prefers ``extra.actions[*].command`` (mini canonical), falls back to
    ``tool_calls[*].function.arguments`` (OpenAI tool-call JSON). This is the
    level-3 (acted) signal only — never used for level-2 references. Actions
    remain separate so a passive target read cannot borrow an unrelated test's
    verification verb. Structured editor paths are retained for relevance.
    """
    parts: list[str] = []

    def _action_text(obj: dict, action_name: object = None) -> str:
        values: list[str] = []
        if isinstance(action_name, str) and action_name:
            values.append(action_name)
        command = obj.get("command")
        if isinstance(command, str) and command not in values:
            values.append(command)
        for key in ("path", "file_path", "file"):
            value = obj.get(key)
            if isinstance(value, str) and value not in values:
                values.append(value)
        return " ".join(values)

    def _structured_call(item: dict) -> str:
        """Normalize provider-neutral Responses/Anthropic function-call content."""
        nested_call = item.get("call")
        nested_name = nested_call.get("name") if isinstance(nested_call, dict) else None
        action_name = item.get("name") or nested_name
        args = item.get("arguments")
        if args is None:
            args = item.get("input")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return " ".join(value for value in (action_name, args)
                                 if isinstance(value, str))
        if isinstance(args, dict):
            text = _action_text(args, action_name)
            if text:
                return text
        return str(action_name) if isinstance(action_name, str) else ""

    extra = msg.get("extra")
    if isinstance(extra, dict):
        for act in extra.get("actions") or []:
            if isinstance(act, dict):
                text = _action_text(act, act.get("name"))
                if text:
                    parts.append(text)
    if not parts:
        # Responses API and Anthropic-style messages carry calls as content/output
        # items rather than Chat Completions ``tool_calls``.  Treat their structured
        # arguments as equivalent model-authored commands.
        for container in (msg.get("content"), msg.get("output")):
            if not isinstance(container, list):
                continue
            for item in container:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").lower()
                if item_type not in {"function_call", "tool_use", "tool_call"}:
                    continue
                text = _structured_call(item)
                if text:
                    parts.append(text)

    if not parts:
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            action_name = fn.get("name")
            if isinstance(args, str):
                try:
                    obj = json.loads(args)
                    text = (
                        _action_text(obj, action_name)
                        if isinstance(obj, dict) else ""
                    )
                    if text:
                        parts.append(text)
                        continue
                except ValueError:
                    pass
                parts.append(" ".join(
                    value for value in (action_name, args) if isinstance(value, str)
                ))
    fc = msg.get("function_call")
    if isinstance(fc, dict) and isinstance(fc.get("arguments"), str):
        args = fc["arguments"]
        action_name = fc.get("name")
        try:
            obj = json.loads(args)
            text = (
                _action_text(obj, action_name) if isinstance(obj, dict) else ""
            )
            parts.append(text or " ".join(
                value for value in (action_name, args) if isinstance(value, str)
            ))
        except ValueError:
            parts.append(" ".join(
                value for value in (action_name, args) if isinstance(value, str)
            ))
    return parts


def _assistant_prose(msg: dict) -> str:
    """Model-authored natural-language text of an assistant message (level-2 only)."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        # Responses/Anthropic content arrays contain output_text/text blocks. Exclude
        # structured function-call arguments from prose so level-2 remains model text.
        text: list[str] = []
        for item in c:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text.append(item["text"])
        return "\n".join(text)
    return ""


def _visible_content(msg: dict) -> str:
    """Flatten model-visible text for string or Responses/provider content arrays."""
    value = msg.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "\n".join(chunks)
    return ""


def _block_entities(
    block: str, _host_file_attr: str | None = None,
) -> tuple[set[str], set[str]]:
    """(file_tokens, symbols) delivered by a GT block.

    Files come only from the exact model-visible bytes: a ``file="..."`` attr
    inside the block or a source-path token in its body, each as both full path
    and basename. Host-side ledger metadata is deliberately excluded because it
    cannot prove that the model received an entity. Symbols are extracted only
    from structured evidence/contract markers (never free-text) to keep false
    references low. ``_host_file_attr`` remains as a compatibility parameter for
    existing callers, but is intentionally never used as receipt evidence.
    """
    files: set[str] = set()
    file_attr = _FILE_ATTR_RE.search(block)
    if file_attr:
        delivered_path = file_attr.group(1)
        files.add(delivered_path)
        files.add(os.path.basename(delivered_path))
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
    # D-Y: the TAGLESS native delivery forms carry the symbol as the trailing token
    # of a `path:line:sym` (bare ripgrep) or `path:line: note: sym …` (D-S
    # compiler-note) row, with NONE of the [CALLERS]/[WITNESS]/def markers above.
    # Extract the leading identifier so the referenced-rung detector is not DARK on
    # the native path (run6: referenced=0 for every producer despite delivered>0).
    for m in re.findall(r"(?m)^[\w./\\+\-]+:\d+:(?:\s*note:\s*)?([A-Za-z_]\w{3,})", block):
        symbols.add(m)
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


def _action_kind(command: str) -> str | None:
    """Classify only repository-changing or verification commands for receipt 3."""
    if _VERIFICATION_COMMAND_RE.search(command):
        return "verification"
    if (
        _MUTATING_COMMAND_RE.search(command)
        or _PYTHON_MUTATING_COMMAND_RE.search(command)
    ):
        return "mutation"
    return None


def _build_v2(
    messages: list[dict],
    *,
    runtime_ledger_path: str | None = None,
    runtime_ledger_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    n = len(messages)

    # Pre-compute per-message model-authored channels once.
    assistant_prose: list[str] = [""] * n
    assistant_cmd: list[list[str]] = [[] for _ in range(n)]
    tool_ordinal: list[int | None] = [None] * n
    # iteration (== Nth tool observation) -> message index of that observation.
    # This is the authoritative delivery-boundary anchor for a sealed row: GT
    # renders a delivery INTO the observation of its own iteration, so the
    # sealed bytes cannot legitimately surface before that message.
    ordinal_to_msg: dict[int, int] = {}
    ord_counter = 0
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            assistant_prose[i] = _assistant_prose(m)
            assistant_cmd[i] = _emitted_commands(m)
        elif role == "tool":
            ord_counter += 1
            tool_ordinal[i] = ord_counter
            ordinal_to_msg[ord_counter] = i

    def _receipt_for(i: int, block: str) -> tuple[int, int | None, int | None, bool]:
        """Receipt ladder for one byte-proven model-visible delivery."""
        files, symbols = _block_entities(block)
        pats = _entity_patterns(files, symbols)
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
            for cmd in assistant_cmd[j]:
                action_kind = _action_kind(cmd)
                relevant = bool(pats and _named_in(cmd, pats))
                if act_idx is None and action_kind is not None and relevant:
                    act_idx = j
                    receipt = max(receipt, 3)
                if not verif and action_kind == "verification" and relevant:
                    verif = True
        return receipt, ref_idx, act_idx, verif

    # 1) Extract every model-visible GT block as a delivery.
    entries: list[dict[str, Any]] = []
    leak_hits: set[str] = set()  # SS-3 defect-4: structural test-identity leaks in GT bytes
    for i, m in enumerate(messages):
        role = m.get("role")
        if role not in ("user", "tool"):
            continue  # assistant = model-authored, system/exit = framing
        content = _visible_content(m)
        if "<gt-" not in content:
            continue
        channel = "brief" if role == "user" else "runtime"
        for mm in _BLOCK_RE.finditer(content):
            leak_hits.update(scan_test_identity_leaks(mm.group(0)))
            block = mm.group(0)
            tag = mm.group(1).lower()
            kind = _TAG_KIND.get(tag, tag)
            fa = _FILE_ATTR_RE.search(block)
            file_attr = fa.group(1) if fa else None
            receipt, ref_idx, act_idx, verif = _receipt_for(i, block)

            entries.append({
                "msg_index": i,
                "tool_ordinal": tool_ordinal[i],
                "kind": kind,
                "delivery_channel": channel,
                "file_path": file_attr,
                "chars": len(block),
                "rendered_text": block,
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
                "legacy_tag": True,
                "span_start": mm.start(),
                "span_end": mm.end(),
                "physical_span_start": mm.start(),
                "physical_span_end": mm.end(),
                "physical_id": _physical_id(i, mm.start(), mm.end()),
            })

    # 3) Join delivered runtime-ledger rows to trajectory blocks.
    join_rate: float | None = None
    ledger_rows_delivered = 0
    ledger_rows_joined = 0
    exact_seal_ambiguities: list[dict[str, Any]] = []
    exact_seal_duplicate_windows: list[dict[str, Any]] = []
    has_runtime_ledger = runtime_ledger_rows is not None or bool(
        runtime_ledger_path and os.path.isfile(runtime_ledger_path)
    )
    if has_runtime_ledger:
        rows = (
            _delivered_rows_from_values(runtime_ledger_rows or [])
            if runtime_ledger_rows is not None
            else _load_delivered_ledger_rows(str(runtime_ledger_path))
        )
        ledger_rows_delivered = len(rows)
        used: set[int] = set()
        merged_aliases: set[int] = set()

        # Track claimed spans without rewriting observation text. Rewriting a
        # buffer shifts every later character offset and corrupts Unicode and
        # same-message reconciliation.
        visible_buffers: list[str] = []
        claimed_spans: list[list[tuple[int, int]]] = []
        for m in messages:
            role, mtype = m.get("role"), m.get("type")
            content = _visible_content(m)
            visible_buffers.append(
                content if content and (
                    role in ("user", "tool", "system")
                    or mtype == "function_call_output"
                ) else ""
            )
            claimed_spans.append([])

        def _basename_match(bf: str | None, rf: str | None) -> bool:
            if not bf or not rf:
                return False
            return os.path.basename(bf) == os.path.basename(rf)

        for row_index, row in enumerate(rows):
            runtime_ledger_index = int(row.get("_runtime_ledger_index", row_index))
            rf = row.get("file_path") or ""
            chars = int(row.get("chars_delivered") or 0)
            it = row.get("iteration")
            chosen: int | None = None
            aliases: list[int] = []
            method = None
            seal = str(row.get("content_sha256_16") or "")
            unjoined_reason = "delivery_unjoined"

            # SS-LIVE Gate 1: a sealed row joins only by its exact rendered
            # bytes. Locate and consume the matching observation window.
            seal_home: int | None = None
            seal_span: tuple[int, int] | None = None
            seal_payload = ""
            if seal and chars > 0:
                all_candidates: list[tuple[int, tuple[int, int]]] = []
                candidates: list[tuple[int, tuple[int, int]]] = []
                for msg_index, content in enumerate(visible_buffers):
                    for span in _locate_seal_spans(content, chars, seal):
                        all_candidates.append((msg_index, span))
                        if any(_spans_overlap(span, prior) for prior in claimed_spans[msg_index]):
                            continue
                        candidates.append((msg_index, span))
                if all_candidates and not candidates:
                    unjoined_reason = "physical_span_unavailable_or_already_claimed"

                if candidates:
                    # Every candidate window is byte-identical by construction:
                    # _locate_seal_spans only returns windows whose UTF-8 bytes
                    # match this row's exact character/byte length AND its
                    # content_sha256_16. The delivered bytes are therefore proven
                    # present in the model-visible stream. When more than one such
                    # window exists it is NOT a completeness failure -- the
                    # delivery reconciles -- but the canonical physical home must
                    # be chosen from the row's AUTHORITATIVE delivery iteration,
                    # not from raw position. GT renders a delivery into the
                    # observation of its own iteration, so the sealed bytes cannot
                    # legitimately surface before that message; an earlier
                    # byte-identical window is the agent independently producing
                    # the same repo bytes (a pre-delivery collision) and must NOT
                    # anchor the delivery (doing so would let _receipt_for count
                    # pre-delivery actions as GT consumption). Select the earliest
                    # unclaimed window at or after the delivery boundary. When the
                    # boundary is unknown (no usable iteration) fall back to the
                    # earliest window. Candidates are generated in (msg_index,
                    # span) ascending order.
                    boundary_msg = (
                        ordinal_to_msg.get(it) if isinstance(it, int)
                        and not isinstance(it, bool) else None
                    )
                    eligible = (
                        [c for c in candidates if c[0] >= boundary_msg]
                        if boundary_msg is not None else candidates
                    )
                    if eligible:
                        seal_home, seal_span = eligible[0]
                        seal_payload = visible_buffers[seal_home][
                            seal_span[0]:seal_span[1]
                        ]
                        claimed_spans[seal_home].append(seal_span)
                        if len(candidates) > 1:
                            exact_seal_duplicate_windows.append({
                                "ledger_row_index": row_index,
                                "content_sha256_16": seal,
                                "chars_delivered": chars,
                                "delivery_boundary_msg_index": boundary_msg,
                                "resolved_physical_id": _physical_id(
                                    seal_home, seal_span[0], seal_span[1]
                                ),
                                "candidate_physical_ids": [
                                    _physical_id(msg_index, span[0], span[1])
                                    for msg_index, span in candidates
                                ],
                            })
                    else:
                        # Byte-identical windows exist but every one is BEFORE the
                        # row's own delivery boundary: a genuine seal/attestation
                        # inconsistency (the sealed bytes surface only ahead of the
                        # iteration that claims to deliver them). This is not
                        # reconcilable -- fail closed.
                        unjoined_reason = "physical_span_precedes_delivery_boundary"
                        exact_seal_ambiguities.append({
                            "ledger_row_index": row_index,
                            "content_sha256_16": seal,
                            "chars_delivered": chars,
                            "delivery_boundary_msg_index": boundary_msg,
                            "candidate_physical_ids": [
                                _physical_id(msg_index, span[0], span[1])
                                for msg_index, span in candidates
                            ],
                        })

                if seal_home is not None and seal_span is not None:
                    # Legacy tags and sealed windows are two representations of
                    # one physical delivery only when the byte-proven sealed
                    # window fully contains the legacy framing. Partial overlap
                    # cannot account for the tag and must remain unjoined.
                    overlapping = [
                        k for k, e in enumerate(entries)
                        if k not in used
                        and e["source"] == "trajectory"
                        and e["msg_index"] == seal_home
                        and e.get("legacy_tag") is True
                        and isinstance(e.get("span_start"), int)
                        and isinstance(e.get("span_end"), int)
                        and _span_contains(
                            seal_span, (int(e["span_start"]), int(e["span_end"]))
                        )
                    ]
                    if overlapping:
                        chosen = overlapping[0]
                        aliases = overlapping[1:]
                        e = entries[chosen]
                        legacy_spans = [
                            (int(entries[k]["span_start"]), int(entries[k]["span_end"]))
                            for k in overlapping
                        ]
                        legacy_span = legacy_spans[0]
                        receipt, ref_idx, act_idx, verif = _receipt_for(
                            seal_home, seal_payload
                        )
                        e["legacy_span_start"], e["legacy_span_end"] = legacy_span
                        e["legacy_spans"] = [list(span) for span in legacy_spans]
                        e["span_start"], e["span_end"] = seal_span
                        e["physical_span_start"] = seal_span[0]
                        e["physical_span_end"] = seal_span[1]
                        e["physical_id"] = _physical_id(
                            seal_home, seal_span[0], seal_span[1]
                        )
                        e["kind"] = str(row.get("layer") or "unknown")
                        e["delivery_channel"] = (
                            "brief" if messages[seal_home].get("role") == "user"
                            else "runtime"
                        )
                        e["file_path"] = str(rf) or None
                        e["rendered_text"] = seal_payload
                        e["chars"] = len(seal_payload)
                        e["content_sha256_16"] = seal
                        e["receipt"] = receipt
                        e["referenced_msg_index"] = ref_idx
                        e["acted_msg_index"] = act_idx
                        e["verification_followup"] = verif
                    else:
                        receipt, ref_idx, act_idx, verif = _receipt_for(
                            seal_home, seal_payload
                        )
                        entries.append({
                            "msg_index": seal_home,
                            "tool_ordinal": tool_ordinal[seal_home],
                            "kind": str(row.get("layer") or "unknown"),
                            "delivery_channel": (
                                "brief" if messages[seal_home].get("role") == "user"
                                else "runtime"
                            ),
                            "file_path": str(rf) or None,
                            "chars": len(seal_payload),
                            "rendered_text": seal_payload,
                            "content_sha256_16": seal,
                            "receipt": receipt,
                            "referenced_msg_index": ref_idx,
                            "acted_msg_index": act_idx,
                            "resolved_state": None,
                            "verification_followup": verif,
                            "joined": None,
                            "ledger_layer": None,
                            "ledger_event_type": None,
                            "ledger_chars": None,
                            "join_method": None,
                            "source": "trajectory",
                            "legacy_tag": False,
                            "span_start": seal_span[0],
                            "span_end": seal_span[1],
                            "physical_span_start": seal_span[0],
                            "physical_span_end": seal_span[1],
                            "physical_id": _physical_id(
                                seal_home, seal_span[0], seal_span[1]
                            ),
                        })
                        chosen = len(entries) - 1
                    leak_hits.update(scan_test_identity_leaks(seal_payload))
                    method = "seal"

            # Backward compatibility only: old runtime ledgers predate seals.
            # They may use the historical file/length/iteration heuristic, but
            # such a join is not SS byte-proof and is labeled accordingly.
            if chosen is None and not seal:
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
                    method = "legacy_content"
                elif isinstance(it, int):
                    icands = [
                        k for k, e in enumerate(entries)
                        if k not in used
                        and e["source"] == "trajectory"
                        and e["tool_ordinal"] == it
                    ]
                    if icands:
                        chosen = icands[0]
                        method = "legacy_iteration"
            if chosen is not None:
                used.add(chosen)
                used.update(aliases)
                merged_aliases.update(aliases)
                ledger_rows_joined += 1
                e = entries[chosen]
                e["joined"] = True
                e["join_method"] = method
                e["ledger_layer"] = row.get("layer")
                e["ledger_event_type"] = row.get("event_type")
                e["ledger_chars"] = chars
                e["runtime_ledger_index"] = runtime_ledger_index
                e["candidate_id"] = row.get("candidate_id")
                e["observation_binding"] = row.get("observation_binding")
                e["opportunity_exempt_reason"] = row.get(
                    "opportunity_exempt_reason"
                )
                if method == "seal":
                    lineage = _typed_lineage_from_row(row)
                    if lineage is not None:
                        e["feature_lineage"] = lineage
            else:
                # host-only delivery not visible as a block — never drop it.
                entries.append({
                    "msg_index": None,
                    "tool_ordinal": it if isinstance(it, int) else None,
                    "kind": str(row.get("layer") or "unknown"),
                    "delivery_channel": "runtime",
                    "file_path": rf or None,
                    "chars": chars,
                    "rendered_text": None,
                    "content_sha256_16": seal or None,
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
                    "legacy_tag": False,
                    "span_start": None,
                    "span_end": None,
                    "physical_span_start": None,
                    "physical_span_end": None,
                    "physical_id": None,
                    "physical_join_reason": unjoined_reason,
                    "runtime_ledger_index": runtime_ledger_index,
                    "candidate_id": row.get("candidate_id"),
                    "observation_binding": row.get("observation_binding"),
                    "opportunity_exempt_reason": row.get(
                        "opportunity_exempt_reason"
                    ),
                })
        # One canonical entry owns each physical interval. Keeping overlapping
        # legacy aliases would make downstream readers rediscover the exact
        # double-count this reconciliation removes.
        if merged_aliases:
            entries = [
                entry for index, entry in enumerate(entries)
                if index not in merged_aliases
            ]
        # trajectory blocks with no ledger match keep joined=False (host silent).
        for e in entries:
            if e["source"] == "trajectory" and e["joined"] is None:
                e["joined"] = False
        join_rate = (
            round(ledger_rows_joined / ledger_rows_delivered, 8)
            if ledger_rows_delivered
            else None
        )

    unjoined_visible_tags = [
        e["physical_id"] for e in entries
        if e.get("source") == "trajectory"
        and e.get("legacy_tag") is True
        and e.get("joined") is False
    ]

    # 4) Aggregate.
    visible = [e for e in entries if e["source"] == "trajectory"]
    physical_groups: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(visible):
        identity = str(entry.get("physical_id") or f"entry:{index}")
        physical_groups.setdefault(identity, []).append(entry)

    def _stable_claim(entry: dict[str, Any]) -> str:
        return json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)

    physical_identity_conflicts = sorted(
        identity for identity, group in physical_groups.items()
        if len({_stable_claim(entry) for entry in group}) > 1
    )
    unique_visible = [
        min(group, key=_stable_claim) for group in physical_groups.values()
    ]
    delivered = len(unique_visible)
    consumed = sum(1 for e in unique_visible if (e["receipt"] or 0) >= 3)
    referenced = sum(1 for e in unique_visible if (e["receipt"] or 0) >= 2)
    verification_followup = sum(
        1 for e in unique_visible if e["verification_followup"]
    )

    per_class: dict[str, dict[str, int]] = {}
    for e in unique_visible:
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
        "receipt_distribution": _receipt_distribution(unique_visible),
        "per_class": per_class,
        "join_rate": join_rate,
        "ledger_rows_delivered": ledger_rows_delivered,
        "ledger_rows_joined": ledger_rows_joined,
        "runtime_ledger_path": runtime_ledger_path,
        "unjoined_visible_tag_count": len(unjoined_visible_tags),
        "unjoined_visible_tag_ids": unjoined_visible_tags,
        "physical_identity_conflict_count": len(physical_identity_conflicts),
        "physical_identity_conflict_ids": physical_identity_conflicts,
        "exact_seal_ambiguity_count": len(exact_seal_ambiguities),
        "exact_seal_ambiguities": exact_seal_ambiguities,
        # Byte-identical duplicate windows for a single sealed delivery. These
        # are RESOLVED to the earliest home (the delivery site) and are a
        # non-blocking diagnostic -- they never make the visible-byte audit
        # incomplete, because the delivered bytes are demonstrably present.
        "exact_seal_duplicate_window_count": len(exact_seal_duplicate_windows),
        "exact_seal_duplicate_windows": exact_seal_duplicate_windows,
        "visible_audit_complete": (
            len(unjoined_visible_tags) == 0
            and not physical_identity_conflicts
            and not exact_seal_ambiguities
            if has_runtime_ledger
            else True
        ),
        # SS-3 defect-4: structural test-identity leaks in the delivered GT bytes.
        # MUST be [] — any hit disqualifies a consumption claim (answer leakage).
        "test_identity_leak_hits": sorted(leak_hits),
        "entries": entries,
    }


def _receipt_distribution(visible: list[dict[str, Any]]) -> dict[str, int]:
    dist = {"1": 0, "2": 0, "3": 0}
    for e in visible:
        r = e["receipt"] or 0
        if 1 <= r <= 3:
            dist[str(r)] += 1
    return dist


PHYSICAL_DELIVERY_AUTHORITY_SCHEMA = "gt.physical_delivery_authority.v1"
PHYSICAL_DELIVERY_BOUND = "PHYSICAL_DELIVERY_BOUND"
BROKEN_PHYSICAL_BINDING = "BROKEN_PHYSICAL_BINDING"


def physical_delivery_authority(ledger: dict[str, Any]) -> dict[str, Any]:
    """Project the consumption ledger's unique claimed spans by runtime-row identity.

    This is the reusable byte authority for chronology, feature metrics, and SS evidence.
    It never relocates bytes: the v2 reconciler already claimed each physical interval.
    Duplicate/conflicting/missing claims remain named broken states rather than becoming
    a weaker ordinal or seal-only guess.
    """
    entries = ledger.get("entries") if isinstance(ledger, dict) else None
    entries = entries if isinstance(entries, list) else []
    conflicts = set(ledger.get("physical_identity_conflict_ids") or [])
    claims: dict[int, list[dict[str, Any]]] = {}
    ledger_only: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        runtime_index = entry.get("runtime_ledger_index")
        if type(runtime_index) is not int or runtime_index < 0:
            continue
        if entry.get("source") == "ledger_only":
            ledger_only[runtime_index] = entry
            continue
        if (
            entry.get("source") == "trajectory"
            and entry.get("joined") is True
            and entry.get("join_method") == "seal"
        ):
            claims.setdefault(runtime_index, []).append(entry)

    deliveries: dict[str, dict[str, Any]] = {}
    all_indices = sorted(set(claims) | set(ledger_only))
    for runtime_index in all_indices:
        row_claims = claims.get(runtime_index, [])
        if len(row_claims) != 1:
            source = ledger_only.get(runtime_index, {})
            deliveries[str(runtime_index)] = {
                "state": BROKEN_PHYSICAL_BINDING,
                "reason": str(
                    source.get("physical_join_reason")
                    or "physical_span_unavailable_or_already_claimed"
                ),
                "runtime_ledger_index": runtime_index,
                "physical_id": None,
                "msg_index": None,
                "span_start": None,
                "span_end": None,
                "rendered_text": None,
                "content_sha256_16": source.get("content_sha256_16"),
                "chars": source.get("ledger_chars", source.get("chars")),
                "candidate_id": source.get("candidate_id"),
                "observation_binding": source.get("observation_binding"),
                "opportunity_exempt_reason": source.get(
                    "opportunity_exempt_reason"
                ),
            }
            continue
        claim = row_claims[0]
        physical_id = claim.get("physical_id")
        if not isinstance(physical_id, str) or not physical_id or physical_id in conflicts:
            deliveries[str(runtime_index)] = {
                "state": BROKEN_PHYSICAL_BINDING,
                "reason": "physical_identity_conflict",
                "runtime_ledger_index": runtime_index,
                "physical_id": physical_id if isinstance(physical_id, str) else None,
                "msg_index": claim.get("msg_index"),
                "span_start": claim.get("span_start"),
                "span_end": claim.get("span_end"),
                "rendered_text": claim.get("rendered_text"),
                "content_sha256_16": claim.get("content_sha256_16"),
                "chars": claim.get("ledger_chars", claim.get("chars")),
                "candidate_id": claim.get("candidate_id"),
                "observation_binding": claim.get("observation_binding"),
                "opportunity_exempt_reason": claim.get(
                    "opportunity_exempt_reason"
                ),
            }
            continue
        deliveries[str(runtime_index)] = {
            "state": PHYSICAL_DELIVERY_BOUND,
            "reason": "unique_claimed_span",
            "runtime_ledger_index": runtime_index,
            "physical_id": physical_id,
            "msg_index": claim.get("msg_index"),
            "span_start": claim.get("span_start"),
            "span_end": claim.get("span_end"),
            "rendered_text": claim.get("rendered_text"),
            "content_sha256_16": claim.get("content_sha256_16"),
            "chars": claim.get("ledger_chars", claim.get("chars")),
            "candidate_id": claim.get("candidate_id"),
            "observation_binding": claim.get("observation_binding"),
            "opportunity_exempt_reason": claim.get("opportunity_exempt_reason"),
            "receipt": claim.get("receipt"),
            "referenced_msg_index": claim.get("referenced_msg_index"),
            "acted_msg_index": claim.get("acted_msg_index"),
            "feature_lineage": claim.get("feature_lineage"),
        }
    broken = [
        index for index, record in deliveries.items()
        if record["state"] == BROKEN_PHYSICAL_BINDING
    ]
    return {
        "schema": PHYSICAL_DELIVERY_AUTHORITY_SCHEMA,
        "valid": not broken,
        "broken_runtime_ledger_indices": broken,
        "deliveries": deliveries,
    }


def _delivered_rows_from_values(
    values: Iterable[object],
) -> list[dict[str, Any]]:
    """Retain delivered rows with their index in the complete parsed ledger."""
    rows: list[dict[str, Any]] = []
    for runtime_index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        if value.get("outcome") != "delivered":
            continue
        try:
            chars = int(value.get("chars_delivered") or 0)
        except (TypeError, ValueError):
            continue
        if chars <= 0:
            continue
        row = dict(value)
        row["_runtime_ledger_index"] = runtime_index
        rows.append(row)
    return rows


def _load_delivered_ledger_rows(path: str) -> list[dict[str, Any]]:
    parsed: list[object] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return _delivered_rows_from_values(parsed)


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
    runtime_ledger_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Delivered -> referenced -> acted receipts from a trajectory.

    Dispatches on shape: mini-swe-agent ``messages[]`` -> v2 receipt ladder;
    legacy pier/OH step list -> v1 (unchanged). ``runtime_ledger_path`` is used
    only on the v2 path (join delivered ledger rows to visible blocks).
    """
    messages = _as_mini_messages(trajectory)
    if messages is not None:
        return _build_v2(
            messages,
            runtime_ledger_path=runtime_ledger_path,
            runtime_ledger_rows=runtime_ledger_rows,
        )
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
