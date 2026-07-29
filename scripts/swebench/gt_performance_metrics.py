#!/usr/bin/env python3
"""GT Performance Metrics — full mandatory metrics collector (sections 1-6, 8-9).

Reads a pier/mini-swe-agent trajectory JSON + GT artifacts (brief.txt, graph.db,
foundational_gate_report.json) and computes ALL performance metrics from sections
1-6, 8-9 of MANDATORY_METRICS.md. Sections 7 (behavioral impact) and 10
(correctness) are handled by gt_behavioral_impact.py and gt_deep_metrics.py.

Usage (standalone):
  python gt_performance_metrics.py <trajectory.json> <artifacts_dir> \\
      [--gold gold_files.txt] [--out metrics.json]

Library import:
  from gt_performance_metrics import compute_performance_metrics
  perf = compute_performance_metrics(trajectory_path, artifacts_dir, gold_files=None)
"""
from __future__ import annotations

import json
import glob
import math
import os
import re
import sys
from typing import Any

try:
    from trajectory_usage import extract_trajectory_usage
except ImportError:
    from scripts.swebench.trajectory_usage import extract_trajectory_usage

# Single source of shell edit-truth (same detector gt_deep_metrics uses):
# gt_localization_efficacy.classify recognizes patch / git-apply / heredoc / sed /
# redirect edits. stdlib-only, same dir. Superset regex kept as a defensive fallback.
try:
    from gt_localization_efficacy import classify as _classify_shell_cmd
except ImportError:  # packaged import path
    try:
        from scripts.swebench.gt_localization_efficacy import classify as _classify_shell_cmd
    except ImportError:  # detector unavailable — degrade to the local superset regex
        _classify_shell_cmd = None

_EDIT_FALLBACK_RE = re.compile(
    r"\bsed\s+-i\b|\bapply_patch\b|\bgit\s+apply\b|(?<![\w])patch\s+[^\n]*<|\btee\b|"
    r"\bcat\s*>>?\s*[\w./+-]|(?:^|\s)>>?\s*[\w./+-]+\."
    r"(?:py|go|ts|tsx|js|jsx|mjs|cjs|rs|java|kt|rb|c|cc|cpp|h|hpp|html|vue|svelte)\b",
    re.I,
)


def _shell_cmd_is_edit(cmd: str) -> bool:
    """True iff a RAW SHELL command mutates a source file (patch/git-apply/heredoc/sed/
    redirect), via gt_localization_efficacy.classify with a superset-regex fallback.
    Structured editor VERBS are decided by the caller's guards, not here — so a
    `view`/`cat`/`grep` read never counts."""
    if not cmd or not cmd.strip():
        return False
    if _classify_shell_cmd is not None:
        try:
            return _classify_shell_cmd(cmd)[0] == "EDIT"
        except Exception:
            pass
    return bool(_EDIT_FALLBACK_RE.search(cmd))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def d8(x):
    """Round to 8 decimal places. Missing data (None / NaN / inf / unparseable)
    -> None (JSON null), NEVER 0.0 — a fabricated measured zero is precision
    theater on the 8-dp mandate (G14). A real number still rounds to 8 dp."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 8)


def _safe_load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


_LEDGER_LAYER_FACT: dict[str, str] = {
    "l3b.evidence": "caller_contract",
    "l3b.contract": "caller_contract",
    "l3.contract": "caller_contract",
    "consensus.scope": "localization",
    "consensus.scope_map": "localization",
    "post_search.localize": "localization",
    "spec.obligation": "obligations",
    "obligation.resurface": "obligations",
    "obligation.unexercised": "obligations",
    "detect.coherence": "recovery",
    "detect.loop": "recovery",
    "recovery": "recovery",
    "nudge": "recovery",
    "edit.syntax": "syntax_result",
    "completion_cert": "submit_refusal",
    "submit_gate": "submit_refusal",
    "cochange": "cochange_prior",
}
_FACT_CLASSES = {
    "obligations", "localization", "def_partition", "caller_contract",
    "syntax_result", "signature_delta", "covering_red", "submit_refusal",
    "cochange_prior", "newfile_precedent", "recovery",
}


def _entry_fact_class(entry: dict) -> str | None:
    lineage = entry.get("feature_lineage")
    layer = str(entry.get("ledger_layer") or entry.get("kind") or "").strip()
    if (
        isinstance(lineage, dict)
        and lineage.get("schema") == "gt.feature_lineage.v1"
        and lineage.get("producer_registration_match") is True
        and lineage.get("fact_class") in _FACT_CLASSES
        and not (
            lineage.get("fact_class") == "covering_red"
            and layer.startswith("verify.horizon.")
            and (
                layer != "verify.horizon.executed"
                or lineage.get("runtime_producer_id") != "covering_runner"
            )
        )
    ):
        return str(lineage["fact_class"])
    if layer.startswith("ga."):
        layer = layer[3:]
    if layer.startswith("gateway."):
        layer = layer.split(".", 1)[1]
    if layer in _FACT_CLASSES:
        return layer
    return _LEDGER_LAYER_FACT.get(layer)


def _visible_receipts(consumption: dict | None, fact_class: str | None = None) -> list[dict]:
    if not isinstance(consumption, dict):
        return []
    ledger_present = bool(consumption.get("runtime_ledger_path"))
    out: list[dict] = []
    for entry in consumption.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("source") != "trajectory":
            continue
        if ledger_present and entry.get("joined") is not True:
            continue
        if int(entry.get("receipt") or 0) < 1:
            continue
        if fact_class is not None and _entry_fact_class(entry) != fact_class:
            continue
        out.append(entry)
    return out


def _authoritative_consumption(consumption: dict | None) -> bool:
    """True when exact-byte runtime-ledger joins are the delivery authority.

    An authoritative ledger may be correctly quiet for a fact class. That empty
    class result must never reopen the legacy tag heuristic.
    """
    return bool(
        isinstance(consumption, dict) and consumption.get("runtime_ledger_path")
    )


def _receipt_message_indices(entries: list[dict]) -> set[int]:
    out: set[int] = set()
    for entry in entries:
        value = entry.get("msg_index")
        if isinstance(value, int) and not isinstance(value, bool):
            out.add(value)
    return out


def _load_consumption(trajectory_path: str, artifacts_dir: str,
                      instance_id: str | None) -> dict:
    """Build the exact-byte receipt ledger for the performance readers."""
    try:
        try:
            from consumption_ledger import ledger_from_trajectory_path
        except ImportError:
            from scripts.swebench.consumption_ledger import ledger_from_trajectory_path
        bases = [os.path.dirname(os.path.abspath(trajectory_path))]
        if artifacts_dir:
            bases.append(os.path.abspath(artifacts_dir))
        hits: list[str] = []
        for base in dict.fromkeys(bases):
            hits.extend(glob.glob(os.path.join(base, "gt_runtime_ledger*.jsonl")))
            hits.extend(glob.glob(os.path.join(base, "**", "gt_runtime_ledger*.jsonl"), recursive=True))
        hits = sorted(dict.fromkeys(hits))
        scoped = [p for p in hits if instance_id and instance_id in os.path.basename(p)]
        ledger = scoped[0] if scoped else (hits[0] if len(hits) == 1 else None)
        return ledger_from_trajectory_path(
            trajectory_path, runtime_ledger_path=ledger
        )
    except Exception as exc:  # collection must be explicit, never a false zero
        return {
            "schema": "gt.consumption_ledger.v2",
            "status": "UNMEASURED",
            "reason": f"consumption_ledger_error:{type(exc).__name__}",
            "entries": [],
        }


# ---------------------------------------------------------------------------
# Trajectory parsing helpers
# ---------------------------------------------------------------------------

# Patterns for detecting file-read commands (cat / head / tail / view)
_READ_FILE_PATTERNS = re.compile(
    r"(?:^|\s)cat\s+([^\s|;&]+)|"
    r"(?:^|\s)head\s+(?:-\S+\s+)*([^\s|;&]+)|"
    r"(?:^|\s)tail\s+(?:-\S+\s+)*([^\s|;&]+)|"
    r"(?:^|\s)less\s+([^\s|;&]+)|"
    r"(?:^|\s)more\s+([^\s|;&]+)|"
    r"(?:^|\s)bat\s+([^\s|;&]+)|"
    r'"command"\s*:\s*"(?:view|open)\s+([^\s"]+)|'
    r'"path"\s*:\s*"([^"]+)"\s*.*"command"\s*:\s*"view"',
    re.I,
)

# Patterns for detecting file edits
_EDIT_FILE_PATTERNS = re.compile(
    r"str_replace_editor.*?(?:\"path\"|path)\s*[:=]\s*[\"']([^\"']+)|"
    r"\"command\"\s*:\s*\"(?:str_replace|create|insert|write|overwrite)\"\s*.*?\"path\"\s*:\s*\"([^\"]+)\"|"
    r"\"path\"\s*:\s*\"([^\"]+)\".*?\"command\"\s*:\s*\"(?:str_replace|create|insert|write|overwrite)\"|"
    r"(?:^|\s)sed\s+-i\s+[^\s]+\s+([^\s|;&]+)|"
    r"tee\s+([^\s|;&]+)\s*<<|"
    r"cat\s*>\s*([^\s|;&<]+)",
    re.I,
)

# Search commands before localization
_SEARCH_PATTERNS = re.compile(
    r"(?:^|\s)(?:grep|rg|find|ag|ack|fd)\s",
    re.I,
)

# Test invocation patterns
_TEST_PATTERNS = re.compile(
    r"(?:^|\s)(?:pytest|py\.test|python\s+-m\s+pytest|"
    r"cargo\s+test|go\s+test|npm\s+test|yarn\s+test|"
    r"vitest|jest|make\s+test|tox|"
    r"python\s+-m\s+unittest|runtests\.py|"
    r"mvn\s+test|gradle\s+test)",
    re.I,
)

# Build/compile failure patterns in observations
_BUILD_FAIL_PATTERNS = re.compile(
    r"(?:error\[E\d+\]|"
    r"SyntaxError:|"
    r"TypeError:|"
    r"error TS\d+:|"
    r"FAILED|"
    r"BUILD FAILED|"
    r"compilation failed|"
    r"cannot find symbol|"
    r"undefined reference to)",
    re.I,
)

# Undo / revert patterns
_REVERT_PATTERNS = re.compile(
    r"(?:^|\s)(?:git\s+checkout\s+--|git\s+restore|git\s+revert)\s",
    re.I,
)

# Nudge delivery pattern
_NUDGE_PATTERN = re.compile(r"<gt-nudge", re.I)

# Contract pattern (CALLERS warnings in <gt-contract> blocks)
_CONTRACT_PATTERN = re.compile(r"<gt-contract>(.*?)</gt-contract>", re.DOTALL | re.I)
_CALLERS_WARNING_PATTERN = re.compile(r"\[CALLERS\]|\bCALLERS?\b\s*:", re.I)

# gt-scope pattern
_SCOPE_PATTERN = re.compile(r"<gt-scope>(.*?)</gt-scope>", re.DOTALL | re.I)

# L1 ranking patterns (from brief.txt)
_L1_RANK_PATTERNS = [
    re.compile(r"^\s*(\d+)[.)]\s+([^\s]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb|php|swift|kt|cs))", re.M),
    re.compile(r"Edit target:\s*([^\s]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb|php|swift|kt|cs))", re.M | re.I),
    # ledger HIGH head "<file> :: <func> — N/3 signals agree" (GT_CONSENSUS_LEDGER; Fable C5)
    re.compile(r"^\s*([^\s]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb|php|swift|kt|cs))\s*::.*signals agree", re.M | re.I),
    re.compile(r"<gt-task-brief>.*?(?:files?|targets?).*?([^\s]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb|php|swift|kt|cs))", re.DOTALL | re.I),
]


def _norm_path(p: str) -> str:
    """Normalize a path to forward slashes, lowercase the extension."""
    if not p:
        return ""
    p = p.replace("\\", "/").strip().strip("'\"")
    return p


def _decode_tool_args_with_presence(tool_calls_json: str) -> tuple[bool, dict]:
    """Decode the first structured arguments object and retain its presence.

    mini-swe-agent stores: tool_calls = [{"function": {"arguments": "<json-string>"}}]
    The outer JSON.dumps() escapes the inner JSON, so we need two decode levels.
    ``(True, {})`` is intentionally distinct from decode failure ``(False, {})``.
    """
    if not tool_calls_json:
        return False, {}
    try:
        tc = json.loads(tool_calls_json)
        if not isinstance(tc, list) or not tc:
            return False, {}
        args_raw = tc[0].get("function", {}).get("arguments", "")
        if isinstance(args_raw, dict):
            return True, args_raw
        if isinstance(args_raw, str):
            decoded = json.loads(args_raw)
            if isinstance(decoded, dict):
                return True, decoded
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    return False, {}


def _decode_tool_args(tool_calls_json: str) -> dict:
    """Return decoded structured arguments, or ``{}`` on any failure.

    This compatibility wrapper preserves the existing API for edit, test,
    search, and revert classification.  View extraction additionally needs the
    presence bit so a valid empty object cannot fall through to model prose.
    """
    return _decode_tool_args_with_presence(tool_calls_json)[1]


_VIEW_VERBS = {"view", "open", "read", "show", "display"}
_EDIT_VERBS_SET = {"str_replace", "create", "insert", "write", "overwrite", "edit", "modify"}
_NON_EDIT_VERBS_SET = {"view", "open", "read", "undo_edit", "undo", "show", "display"}


def _extract_shell_viewed_file(command: str) -> str | None:
    """Return the file operand of one read-only shell command, if present."""
    if not command:
        return None

    # ``sed -n`` consumes an address/program before its file operand.  Treating
    # the first token after ``-n`` as the file loses every normal quoted range
    # such as ``sed -n '596,1000p' sh.py``.
    file_token = r"[^\s|;&<>\"'\\]+"
    match = re.search(
        r"(?:^|[;&|]\s*)sed\s+-n(?:\s+-e)?\s+"
        r"(?:'[^']*'|\"[^\"]*\"|\S+)\s+(?:--\s+)?(" + file_token + r")",
        command,
        re.I,
    )
    if match:
        return _norm_path(match.group(1))

    match = re.search(
        r"(?:^|\s)(?:cat|head|tail|bat|less|more)\s+"
        r"(?:-[^\s]+\s+)*(" + file_token + r")",
        command,
        re.I,
    )
    if match:
        path = match.group(1).strip()
        if "/" in path or "." in path:
            return _norm_path(path)
    return None


def _extract_viewed_file(tool_calls_json: str, full_cmd: str) -> str | None:
    """Extract a file path from a read command.

    Tries structured args first (tool_calls JSON), then regex on the full text.
    Returns None if not found.
    """
    # 1. Structured parse of tool_calls
    structured_present, args = _decode_tool_args_with_presence(tool_calls_json)
    if structured_present:
        verb = str(args.get("command") or "").lower()
        path = str(args.get("path") or args.get("file_path") or "")
        if path and verb in _VIEW_VERBS:
            return _norm_path(path)
        if path and verb not in _EDIT_VERBS_SET and verb != "":
            # command might be the shell command itself (bash agent)
            # or "view" already captured above — only return for explicit view verbs
            pass
        # Some agents use just {"command": "cat src/foo.py"} as a bash call.
        # A successfully decoded tool call is authoritative: assistant prose in
        # ``full_cmd`` is not another command surface and must not manufacture a
        # view (for example, "look more broadly" -> a fake ``broadly.`` path).
        bash_cmd = str(args.get("command") or "")
        return _extract_shell_viewed_file(bash_cmd)

    # 2. Regex fallback on the full concatenated command string
    # Try unescaped structured patterns first
    for pat in (
        r'"command"\s*:\s*"(?:view|open|read)"\s*,\s*"path"\s*:\s*"([^"]+)"',
        r'"path"\s*:\s*"([^"]+)"\s*,\s*"command"\s*:\s*"(?:view|open|read)"',
    ):
        m = re.search(pat, full_cmd, re.I | re.DOTALL)
        if m:
            return _norm_path(m.group(1))

    # Try escaped JSON (\\\"command\\\"...) produced by json.dumps(tool_calls)
    for pat in (
        r'\\"command\\"\s*:\s*\\"(?:view|open|read)\\"\s*,\s*\\"path\\"\s*:\s*\\"([^\\]+)\\"',
        r'\\"path\\"\s*:\s*\\"([^\\]+)\\"\s*,\s*\\"command\\"\s*:\s*\\"(?:view|open|read)\\"',
    ):
        m = re.search(pat, full_cmd, re.I | re.DOTALL)
        if m:
            return _norm_path(m.group(1))

    # Shell fallback is reserved for genuinely unstructured/legacy records.
    return _extract_shell_viewed_file(full_cmd)


def _extract_edited_file(tool_calls_json: str, full_cmd: str) -> str | None:
    """Extract a file path from an edit command.

    Tries structured args first, then regex on the full text.
    Returns None if not found.
    """
    # 1. Structured parse
    args = _decode_tool_args(tool_calls_json)
    if args:
        verb = str(args.get("command") or "").lower()
        path = str(args.get("path") or args.get("file_path") or "")
        if path and verb in _EDIT_VERBS_SET:
            return _norm_path(path)

    # 2. Regex on full_cmd (unescaped JSON)
    for pat in (
        r'"command"\s*:\s*"(?:str_replace|create|insert|write|overwrite|edit)"\s*.*?"path"\s*:\s*"([^"]+)"',
        r'"path"\s*:\s*"([^"]+)".*?"command"\s*:\s*"(?:str_replace|create|insert|write|overwrite|edit)"',
    ):
        m = re.search(pat, full_cmd, re.I | re.DOTALL)
        if m:
            return _norm_path(m.group(1))

    # Escaped JSON
    for pat in (
        r'\\"command\\"\s*:\s*\\"(?:str_replace|create|insert|write|overwrite|edit)\\".*?\\"path\\"\s*:\s*\\"([^\\]+)\\"',
        r'\\"path\\"\s*:\s*\\"([^\\]+)\\".*?\\"command\\"\s*:\s*\\"(?:str_replace|create|insert|write|overwrite|edit)\\"',
    ):
        m = re.search(pat, full_cmd, re.I | re.DOTALL)
        if m:
            return _norm_path(m.group(1))

    # Shell patterns. Run them on the DECODED bash command first (clean, unescaped),
    # then on full_cmd. On full_cmd the mini-swe tool_calls JSON double-escapes the
    # command, so a path capture there can trail JSON junk (`src/x.py\"}`) — hence the
    # file-token classes below all exclude quotes/backslash, and the decoded command is
    # preferred. sed -i [flags] 'SCRIPT' FILE: the SCRIPT is usually QUOTED and can
    # contain spaces (e.g. 's/from typing import Any/.../'), so the FILE is the bare
    # token AFTER the closing quote — NOT the first whitespace token inside the script
    # (the pre-fix regex captured e.g. "typing" and corrupted steps_to_gold_edit).
    bash_cmd = str(args.get("command") or "") if args else ""
    _F = r"[^\s|;&><\"'\\]+"  # a bare file token: no whitespace, redirects, quotes, backslash
    for text in (bash_cmd, full_cmd):
        if not text:
            continue
        m = re.search(r"sed\s+-i\S*\s+(?:-e\s+)*(['\"]).*?\1\s+(" + _F + r")", text, re.I | re.S)
        if m:
            return _norm_path(m.group(2))
        m = re.search(r"sed\s+-i\S*\s+[^\s'\"]+\s+(" + _F + r")", text, re.I)  # unquoted script
        if m:
            return _norm_path(m.group(1))
        m = re.search(r"tee\s+(?:-a\s+)?(" + _F + r")\s*<<", text, re.I)
        if m:
            return _norm_path(m.group(1))
        m = re.search(r"cat\s*>\s*(" + _F + r")", text, re.I)
        if m:
            return _norm_path(m.group(1))
        m = re.search(r"patch\s+(?:-\S+\s+)*(" + _F + r"\.(?:py|go|ts|js|java|rs|cpp|c|h|rb))", text, re.I)
        if m:
            return _norm_path(m.group(1))
        # `>`/`>>` redirect to a source file (e.g. `printf ... >> src/x.py`). The `cat >`
        # form is handled above; this catches the general redirect that _is_edit_command
        # now recognizes, so the gold-gated efficacy metric can attribute the edited file.
        # The redirect operator must be at a token boundary so `2>err`/`->x` never match.
        m = re.search(r"(?:^|\s)>>?\s*(" + _F + r")", text)
        if m:
            tgt = m.group(1)
            if tgt != "/dev/null" and re.search(
                r"\.(?:py|go|ts|tsx|js|jsx|rs|java|kt|rb|c|cc|cpp|h|hpp)$", tgt, re.I
            ):
                return _norm_path(tgt)
    return None


def _is_edit_command(tool_calls_json: str, full_cmd: str) -> bool:
    """True if cmd is a mutating edit, not a view."""
    # 1. Structured parse
    args = _decode_tool_args(tool_calls_json)
    if args:
        verb = str(args.get("command") or "").lower()
        if verb in _NON_EDIT_VERBS_SET:
            return False
        if verb in _EDIT_VERBS_SET:
            return True
        # bash command might contain edit patterns — complete detector (adds git apply,
        # `patch … <`, heredoc, redirects) instead of the old blind token subset.
        bash_cmd = str(args.get("command") or "")
        if bash_cmd and _shell_cmd_is_edit(bash_cmd):
            return True
        return False

    # 2. Regex fallback
    # Check for view verbs first (negative)
    for pat in (
        r'"command"\s*:\s*"(?:view|open|read|undo_edit|undo)"',
        r'\\"command\\"\s*:\s*\\"(?:view|open|read|undo_edit|undo)\\"',
    ):
        if re.search(pat, full_cmd, re.I):
            return False
    # Edit verbs
    for pat in (
        r'"command"\s*:\s*"(?:str_replace|create|insert|write|overwrite|edit)"',
        r'\\"command\\"\s*:\s*\\"(?:str_replace|create|insert|write|overwrite|edit)\\"',
    ):
        if re.search(pat, full_cmd, re.I):
            return True
    # Shell patterns — complete detector (patch/git-apply/heredoc/sed/redirect).
    return _shell_cmd_is_edit(full_cmd)


def _is_test_command(tc_json: str, full_cmd: str) -> bool:
    """Check tool_calls args (decoded) first, then fall back to regex on full text."""
    args = _decode_tool_args(tc_json)
    if args:
        bash_cmd = str(args.get("command") or "")
        if bash_cmd and _TEST_PATTERNS.search(bash_cmd):
            return True
    return bool(_TEST_PATTERNS.search(full_cmd))


def _is_search_command(tc_json: str, full_cmd: str) -> bool:
    """Check tool_calls args (decoded) first, then fall back to regex on full text."""
    args = _decode_tool_args(tc_json)
    if args:
        bash_cmd = str(args.get("command") or "")
        if bash_cmd and _SEARCH_PATTERNS.search(bash_cmd):
            return True
    return bool(_SEARCH_PATTERNS.search(full_cmd))


def _is_revert_command(tc_json: str, full_cmd: str) -> bool:
    """Check tool_calls args (decoded) first, then fall back to regex on full text."""
    args = _decode_tool_args(tc_json)
    if args:
        bash_cmd = str(args.get("command") or "")
        if bash_cmd and _REVERT_PATTERNS.search(bash_cmd):
            return True
    return bool(_REVERT_PATTERNS.search(full_cmd))


def _extract_scope_files(content: str) -> list[str]:
    """Extract file paths listed in <gt-scope> blocks."""
    files = []
    for block in _SCOPE_PATTERN.findall(content):
        # Look for lines that look like file paths
        for line in block.splitlines():
            line = line.strip()
            m = re.search(r"([^\s]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb|php|swift|kt|cs))", line)
            if m:
                files.append(_norm_path(m.group(1)))
    return files


def _parse_l1_ranking(brief_txt: str) -> list[str]:
    """Extract the ranked file list from brief.txt. Returns paths in rank order."""
    ranked = []
    for pat in _L1_RANK_PATTERNS:
        matches = pat.findall(brief_txt)
        for m in matches:
            if isinstance(m, tuple):
                # The last non-empty group is the file path
                path = next((g for g in reversed(m) if g), "")
            else:
                path = m
            path = _norm_path(path)
            if path and path not in ranked:
                ranked.append(path)
        if ranked:
            break
    return ranked


# Metrics whose value is computed against the gold-file set. Under a
# submission-proxy "gold" (the agent's OWN diff) every one is CIRCULAR — it
# measures agreement with whatever the agent did, even on a FAILED task — so they
# are emitted as null (G1), never a fabricated 0/1 that reads as a real score.
_GOLD_DERIVED_FIELDS: dict[str, tuple[str, ...]] = {
    "localization": (
        "gold_in_L1_top_k", "gold_rank", "gold_never_reached", "first_gold_view_step",
        "files_to_gold_view", "steps_to_gold_view", "files_to_gold_edit",
        "steps_to_gold_edit", "localization_precision", "localization_recall",
        "false_file_rate", "exploration_ratio", "gold_view_precision", "wasted_views",
        "navigation_directness", "self_localization_needed",
    ),
    "edit_quality": (
        "edit_attempts_per_gold", "first_edit_correctness",
    ),
    "scope_completeness": (
        "scope_coverage", "scope_excess", "multi_file_discovery", "scope_gap_files",
    ),
    "token_efficiency": ("tokens_per_gold_edit", "wasted_token_rate"),
}


def _null_gold_derived(out: dict) -> None:
    """Set every gold-derived metric to null (G1) — used when the "gold" is only a
    submission proxy, so no circular metric masquerades as a measured value."""
    for section, fields in _GOLD_DERIVED_FIELDS.items():
        sec = out.get(section)
        if isinstance(sec, dict):
            for k in fields:
                if k in sec:
                    sec[k] = None


def _applicability(
    applicable: bool, predicate: str, reason: str,
) -> dict[str, object]:
    """Return the explicit contract consumed by ``gt_run_metrics``."""
    return {
        "applicable": applicable,
        "predicate": predicate,
        "reason": reason,
    }


_RIGHT_CENSORED_LOCALIZATION: dict[str, tuple[str, str, str]] = {
    "files_to_gold_view": ("first_gold_view", "unique_files", "_unique_viewed"),
    "steps_to_gold_view": ("first_gold_view", "assistant_steps", "_terminal_step"),
    "files_to_gold_edit": ("first_gold_edit", "unique_files", "_unique_edited"),
    "steps_to_gold_edit": ("first_gold_edit", "assistant_steps", "_terminal_step"),
}
_RIGHT_CENSOR_TERMINALS = {
    "submitted": "Submitted",
    "limitsexceeded": "LimitsExceeded",
}


def build_metric_applicability(
    performance: dict,
    behavioral_impact: dict | None = None,
    *,
    behavioral_collection_succeeded: bool = False,
) -> dict[str, dict[str, dict[str, object]]]:
    """Build auditable applicability from objective metric denominators.

    An absent or failed collection produces no non-applicability claim: the run
    aggregator will correctly classify the missing value as ``UNMEASURED``.
    ``applicable=True`` is retained for known denominators whose outcome was not
    observed (for example, true gold exists but the agent never reached it).
    """
    contracts: dict[str, dict[str, dict[str, object]]] = {}

    if (
        isinstance(performance, dict)
        and not performance.get("error")
        and performance.get("status") != "UNMEASURED"
    ):
        n_gold = int(performance.get("n_gold_files") or 0)
        gold_source = str(performance.get("gold_source") or "none")
        has_true_gold = gold_source in {"true_gold", "dataset_gold"} and n_gold > 0
        gold_contract = _applicability(
            has_true_gold,
            "n_gold_files > 0",
            "true gold file denominator is available"
            if has_true_gold else "true gold file denominator is absent",
        )
        for section, fields in _GOLD_DERIVED_FIELDS.items():
            contracts.setdefault(section, {}).update(
                {field: dict(gold_contract) for field in fields}
            )

        localization = performance.get("localization") or {}
        n_edited = int(localization.get("_unique_edited") or 0)
        n_viewed = int(localization.get("_unique_viewed") or 0)
        for metric in ("localization_precision", "false_file_rate", "exploration_ratio"):
            contracts.setdefault("localization", {})[metric] = _applicability(
                has_true_gold and n_edited > 0,
                "true_gold_available and unique_edited_files > 0",
                "true gold and at least one edited file provide the metric denominator"
                if has_true_gold and n_edited > 0
                else "true gold or the edited-file denominator is absent",
            )
        contracts.setdefault("localization", {})["gold_view_precision"] = _applicability(
            has_true_gold and n_viewed > 0,
            "true_gold_available and unique_viewed_files > 0",
            "true gold and at least one viewed file provide the metric denominator"
            if has_true_gold and n_viewed > 0
            else "true gold or the viewed-file denominator is absent",
        )
        n_gold_edited = int(localization.get("_gold_edited_count") or 0)
        terminal_status = _RIGHT_CENSOR_TERMINALS.get(
            str(performance.get("terminal_status") or "").casefold()
        )
        if has_true_gold and terminal_status is not None:
            for metric, (event, clock, bound_field) in (
                _RIGHT_CENSORED_LOCALIZATION.items()
            ):
                if localization.get(metric) is not None:
                    continue
                bound = localization.get(bound_field)
                if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                    continue
                contracts["localization"][metric]["observation"] = {
                    "state": "RIGHT_CENSORED",
                    "event": event,
                    "clock": clock,
                    "lower_bound": bound,
                    "terminal_horizon": bound,
                    "terminal_status": terminal_status,
                }
        first_edit_applicable = has_true_gold and n_gold_edited > 0
        contracts.setdefault("edit_quality", {})["first_edit_correctness"] = (
            _applicability(
                first_edit_applicable,
                "gold_files_edited > 0",
                "at least one true-gold file was edited"
                if first_edit_applicable else "no true-gold file was edited",
            )
        )
        contracts.setdefault("edit_quality", {})["edit_attempts_per_gold"] = (
            _applicability(
                first_edit_applicable,
                "true_gold_files_edited > 0",
                "at least one true-gold file was edited"
                if first_edit_applicable else "no true-gold file was edited",
            )
        )
        token_efficiency = performance.get("token_efficiency") or {}
        n_token_gold_edited = int(token_efficiency.get("_n_gold_edited") or 0)
        token_ratio_applicable = has_true_gold and n_token_gold_edited > 0
        contracts.setdefault("token_efficiency", {})[
            "tokens_per_gold_edit"
        ] = _applicability(
            token_ratio_applicable,
            "true_gold_available and gold_files_edited > 0",
            (
                "one or more true-gold files were edited"
                if token_ratio_applicable
                else "no true-gold file was edited"
                if has_true_gold
                else "true gold file denominator is absent"
            ),
        )

        scope = performance.get("scope_completeness") or {}
        scope_edits = int(scope.get("_total_edited_count") or 0)
        contracts.setdefault("scope_completeness", {})["scope_excess"] = _applicability(
            has_true_gold and scope_edits > 0,
            "true_gold_available and unique_edited_files > 0",
            "true gold and at least one edited file provide the metric denominator"
            if has_true_gold and scope_edits > 0
            else "true gold or the edited-file denominator is absent",
        )
        contracts.setdefault("scope_completeness", {})["multi_file_discovery"] = (
            _applicability(
                has_true_gold and n_gold > 1,
                "true_gold_files > 1",
                "the task has multiple true-gold files"
                if has_true_gold and n_gold > 1
                else "the task does not have multiple true-gold files",
            )
        )

        interface = performance.get("interface_preservation") or {}
        n_contract_warnings = int(interface.get("_contract_warning_count") or 0)
        contracts.setdefault("interface_preservation", {})[
            "contract_compliance_rate"
        ] = _applicability(
            n_contract_warnings > 0,
            "contract_warning_count > 0",
            "at least one caller-contract warning was delivered"
            if n_contract_warnings > 0 else "no caller-contract warning was delivered",
        )

        # p2p_regression_rate and caller_breakage_count are nullable verifier-derived
        # metrics.  Emit a non-applicability contract ONLY when a VALID verifier truth
        # supplies the objective denominator: absent/invalid verifier truth is a
        # collection failure that must stay UNMEASURED (no contract), never laundered
        # into N/A.  Applicability keys on the verifier's own p2p denominator and
        # caller-join completeness — never on the null value — so a value that should
        # exist but is missing (applicable=True + null) fails closed as UNMEASURED.
        if bool(interface.get("_verifier_truth_valid")):
            p2p_total = interface.get("_p2p_total")
            p2p_has_denominator = (
                isinstance(p2p_total, int) and not isinstance(p2p_total, bool)
                and p2p_total > 0
            )
            contracts["interface_preservation"]["p2p_regression_rate"] = _applicability(
                p2p_has_denominator,
                "verifier_truth_valid and p2p_total > 0",
                "a valid verifier truth reports a PASS_TO_PASS denominator"
                if p2p_has_denominator
                else "a valid verifier truth reports no PASS_TO_PASS denominator",
            )
            caller_join_complete = bool(interface.get("_caller_join_complete"))
            caller_reason = interface.get("_caller_breakage_unmeasured_reason")
            contracts["interface_preservation"]["caller_breakage_count"] = _applicability(
                caller_join_complete,
                "caller_aware_verifier_join_complete",
                "the caller-aware verifier join completed"
                if caller_join_complete
                else (
                    caller_reason
                    if isinstance(caller_reason, str) and caller_reason.strip()
                    else "caller_aware_verifier_join_absent"
                ),
            )

        edit_quality = performance.get("edit_quality") or {}
        total_edits = int(edit_quality.get("_total_edits") or 0)
        contracts.setdefault("edit_quality", {})["edit_revert_rate"] = _applicability(
            total_edits > 0,
            "total_edits > 0",
            "at least one edit exists" if total_edits > 0 else "no edit denominator exists",
        )

        stuck = performance.get("stuck_recovery") or {}
        nudge_recoveries = stuck.get("_nudge_recoveries") or []
        contracts.setdefault("stuck_recovery", {})["nudge_recovery_steps"] = _applicability(
            bool(nudge_recoveries),
            "nudge_recovery_observations > 0",
            "at least one nudge recovery interval exists"
            if nudge_recoveries else "no nudge recovery interval exists",
        )

        verify = performance.get("verify_before_submit") or {}
        verify_edits = int(verify.get("_total_edits") or 0)
        for metric in ("test_edit_ratio", "verify_gap"):
            contracts.setdefault("verify_before_submit", {})[metric] = _applicability(
                verify_edits > 0,
                "total_edits > 0",
                "at least one edit exists" if verify_edits > 0 else "no edit denominator exists",
            )
        n_obligations = int(verify.get("_obligations_delivered") or 0)
        contracts.setdefault("verify_before_submit", {})[
            "obligation_test_rate"
        ] = _applicability(
            n_obligations > 0,
            "obligations_delivered > 0",
            "at least one obligation was delivered"
            if n_obligations > 0 else "no obligation was delivered",
        )

        attribution = performance.get("gt_attribution") or {}
        n_nudges = int(attribution.get("_nudges_delivered") or 0)
        contracts.setdefault("gt_attribution", {})["nudge_action_rate"] = (
            _applicability(
                n_nudges > 0,
                "nudges_delivered > 0",
                "at least one recovery nudge was delivered"
                if n_nudges > 0 else "no recovery nudge was delivered",
            )
        )
        attribution_denominators = {
            "L1_followed_rate": (int(attribution.get("_l1_count") or 0), "L1_top5_count > 0"),
            "contract_consulted_rate": (int(attribution.get("_edits_count") or 0), "total_edits > 0"),
            "obligation_completion_rate": (
                int(attribution.get("_obligations_delivered") or 0),
                "obligations_delivered > 0",
            ),
            "scope_chain_followed": (
                int(attribution.get("_scope_files_count") or 0), "scope_files_listed > 0",
            ),
        }
        for metric, (denominator, predicate) in attribution_denominators.items():
            contracts.setdefault("gt_attribution", {})[metric] = _applicability(
                denominator > 0,
                predicate,
                "metric denominator exists" if denominator > 0 else "metric denominator is absent",
            )

    if (
        behavioral_collection_succeeded
        and isinstance(behavioral_impact, dict)
        and not behavioral_impact.get("collection_error")
    ):
        total_deliveries = int(behavioral_impact.get("total_deliveries") or 0)
        total_pivots = int(behavioral_impact.get("total_pivots") or 0)
        per_tag = behavioral_impact.get("per_tag_impact") or {}
        recovery_deliveries = 0
        if isinstance(per_tag, dict):
            recovery = per_tag.get("recovery") or {}
            if isinstance(recovery, dict):
                recovery_deliveries = int(recovery.get("total") or 0)
        section = contracts.setdefault("behavioral_impact", {})
        section["impact_rate"] = _applicability(
            total_deliveries > 0,
            "total_deliveries > 0",
            "at least one byte-joined GT delivery was observed"
            if total_deliveries > 0 else "no byte-joined GT delivery was observed",
        )
        section["per_tag_impact"] = _applicability(
            total_deliveries > 0,
            "total_deliveries > 0",
            "at least one byte-joined GT delivery was observed"
            if total_deliveries > 0 else "no byte-joined GT delivery was observed",
        )
        section["gt_tokens_per_pivot"] = _applicability(
            total_pivots > 0,
            "total_pivots > 0",
            "at least one diagnostic action pivot was observed"
            if total_pivots > 0 else "no diagnostic action pivot was observed",
        )
        section["nudge_compliance_rate"] = _applicability(
            recovery_deliveries > 0,
            "recovery_deliveries > 0",
            "at least one byte-joined recovery nudge was delivered"
            if recovery_deliveries > 0 else "no byte-joined recovery nudge was delivered",
        )

    return contracts


def _parse_gold_from_diff(submission: str) -> list[str]:
    """Parse gold files from a git diff submission."""
    files = []
    for m in re.finditer(r"diff --git a/(\S+) b/(\S+)", submission):
        p = _norm_path(m.group(2))
        if p and p not in files:
            files.append(p)
    return files


def _parse_gold_from_file(path: str) -> list[str]:
    """Parse gold files from a text file (one path per line)."""
    if not path or not os.path.exists(path):
        return []
    files = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = _norm_path(line.strip())
                if line and not line.startswith("#"):
                    files.append(line)
    except OSError:
        pass
    return files


def _is_test_file(path: str) -> bool:
    """Is this path a TEST file? Mirrors the graph indexer's is_test classification
    (gt_intel_lean.is_critical_path exclusion, gt-index/store.go is_test) plus the
    curation-smoke test-path hints, so the gold set excludes tests EXACTLY the way
    the rest of GT does — no bespoke rule that could drift from the product.
    """
    p = _norm_path(path).lower()
    base = p.rsplit("/", 1)[-1]
    if (base.startswith("test_") or base.startswith("conftest")
            or "_test." in base or ".test." in base or ".spec." in base):
        return True
    if ("/test/" in p or "/tests/" in p or p.startswith("tests/") or p.startswith("test/")
            or "/__tests__/" in p or "/spec/" in p):
        return True
    return False


# The SUBMITTED PATCH is the strongest edit-truth authority (mirrors task_truth.py
# `_first_edit_step_for_paths` / `_EDIT_SHAPE_RE`): command inference over the tool
# calls MISSES edits applied via heredoc / `git apply` / multi-file patches and
# attributes at most ONE file per action.  These two headers together recover every
# edited file whether the diff carries git extended headers (`diff --git a/… b/…`)
# or is a plain unified diff (`--- … / +++ …`, from `diff -u` / `patch`).
_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
_DIFF_PLUS_RE = re.compile(r"^\+\+\+\s+(\S+)", re.M)


def extract_edit_targets(diff_text: str) -> set[str]:
    """Parse the SET of edited file paths from a unified / git diff.

    Reads BOTH the ``diff --git a/… b/…`` post-image name and the ``+++`` hunk header,
    strips a leading ``b/``, and skips ``/dev/null`` (a delete's post-image).  Returns
    normalized, de-duplicated paths.  Never raises — a malformed/empty diff yields an
    empty set (the caller then falls back to command inference).
    """
    targets: set[str] = set()
    if not diff_text:
        return targets
    for m in _DIFF_GIT_RE.finditer(diff_text):
        b = m.group(2)
        if b and b != "/dev/null":
            targets.add(_norm_path(b))
    for m in _DIFF_PLUS_RE.finditer(diff_text):
        p = m.group(1)
        if not p or p == "/dev/null":
            continue
        if p.startswith("b/"):
            p = p[2:]
        if p and p != "/dev/null":
            targets.add(_norm_path(p))
    targets.discard("")
    targets.discard("/dev/null")
    return targets


# Predeclared, ARM-SYMMETRIC junk filter for the edited-file set.  Gold is source-only
# (``gold_files_from_dataset`` drops tests), so ``n_edited`` must exclude the same
# non-source noise on BOTH arms — the rule is fixed here, never keyed on GT on/off,
# task id, or repo shape.
_JUNK_EDIT_SUFFIXES = (".orig", ".bak", ".rej", ".swp", ".csv", ".log")
# YYYYMMDD-HHMMSS_ generated files (e.g. 20260721-153000_report.py).
_TIMESTAMPED_GEN_RE = re.compile(r"\d{8}-\d{6}_")


def _is_junk_edit_path(path: str) -> bool:
    """Non-source edit artifact that must be dropped from ``n_edited`` SYMMETRICALLY.

    Predeclared, arm-independent rule (no task/repo keying): editor / patch backups
    (``.orig/.bak/.rej/.swp``), data / log spew (``.csv/.log``), a bare ``console``
    sink, and timestamped generated files (``YYYYMMDD-HHMMSS_*``).  None of these can
    ever be gold, so counting them would only inflate the false-file denominator.
    """
    p = _norm_path(path).lower()
    if not p:
        return True
    base = p.rsplit("/", 1)[-1]
    if base == "console":
        return True
    if p.endswith(_JUNK_EDIT_SUFFIXES):
        return True
    if _TIMESTAMPED_GEN_RE.search(base):
        return True
    return False


def gold_files_from_dataset(instance_id: str, jsonl_path: str) -> list[str]:
    """TRUE gold SOURCE files for one task, from the SWE-bench dataset jsonl.

    Parses the record's gold ``patch`` (the human fix — NOT ``test_patch``) diff
    headers into repo-relative b-paths and DROPS test files (``_is_test_file``),
    so the result is the source-file gold set. Order-preserving, de-duplicated.

    OFFLINE-ONLY (HARD LAW): the return value is a metric-side artifact. It is fed
    to the localization reader as ``gold_source="dataset_gold"`` and must never be
    surfaced to the model. Returns ``[]`` on any miss (absent file, unknown id,
    unparseable line) — never raises, never fabricates.
    """
    if not instance_id or not jsonl_path or not os.path.exists(jsonl_path):
        return []
    files: list[str] = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # cheap pre-filter; the exact id equality below guards prefix collisions
                if instance_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("instance_id") != instance_id:
                    continue
                patch = rec.get("patch") or ""
                for _a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", patch, re.M):
                    fp = _norm_path(b)
                    if fp and not _is_test_file(fp) and fp not in files:
                        files.append(fp)
                break
    except OSError:
        return []
    return files


def _parse_patch_stats(submission: str) -> tuple[int, int]:
    """Returns (additions + deletions, files_in_patch) from a diff string."""
    if not submission:
        return 0, 0
    files = set()
    additions = 0
    deletions = 0
    for line in submission.splitlines():
        if line.startswith("diff --git"):
            m = re.search(r"diff --git a/(\S+)", line)
            if m:
                files.add(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions + deletions, len(files)


# ---------------------------------------------------------------------------
# Core: parse trajectory into a timeline of events
# ---------------------------------------------------------------------------

def _parse_timeline(messages: list[dict]) -> list[dict]:
    """Convert messages to a flat timeline of events.

    Each event:
      role: 'assistant' | 'observation'
      step: int (assistant steps only)
      cmd: str (raw command text for assistant steps)
      content: str (for observations)
      viewed_file: str | None
      edited_file: str | None
      is_edit: bool
      is_test: bool
      is_search: bool
      is_revert: bool
      gt_tags: list[str] (for observations)
      has_gt: bool
      nudge_delivered: bool
      contract_content: str (contract block text)
      scope_files: list[str]
      has_build_fail: bool
    """
    timeline = []
    step = 0

    for msg_index, m in enumerate(messages):
        role = m.get("role", "")
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = ""

        if role == "assistant":
            step += 1
            # tool_calls carries the actual command
            tc = m.get("tool_calls")
            tc_json = json.dumps(tc) if tc else ""
            # full_cmd combines tool_calls JSON and the thought content for regex fallbacks
            full_cmd = tc_json + " " + (content if isinstance(content, str) else "")

            is_edit = _is_edit_command(tc_json, full_cmd)
            viewed_file = _extract_viewed_file(tc_json, full_cmd) if not is_edit else None
            edited_file = _extract_edited_file(tc_json, full_cmd) if is_edit else None

            timeline.append({
                "role": "assistant",
                "msg_index": msg_index,
                "step": step,
                "timestamp_ms": (
                    int(float((m.get("extra") or {}).get("timestamp")) * 1000)
                    if isinstance(m.get("extra"), dict)
                    and isinstance((m.get("extra") or {}).get("timestamp"), (int, float))
                    else None
                ),
                "cmd": full_cmd,
                "tc_json": tc_json,
                "viewed_file": viewed_file,
                "edited_file": edited_file,
                "is_edit": is_edit,
                "is_test": _is_test_command(tc_json, full_cmd),
                "is_search": _is_search_command(tc_json, full_cmd),
                "is_revert": _is_revert_command(tc_json, full_cmd),
                "gt_tags": [],
                "has_gt": False,
                "nudge_delivered": False,
                "contract_content": "",
                "scope_files": [],
                "has_build_fail": False,
            })

        elif role in ("tool", "user"):
            gt_tags_m = re.findall(r"<gt-(evidence|contract|scope|nudge|verify|obligations)", content, re.I)
            contracts = _CONTRACT_PATTERN.findall(content)
            contract_text = "\n".join(contracts)
            scope_files = _extract_scope_files(content)
            has_build_fail = bool(_BUILD_FAIL_PATTERNS.search(content[:2000]))  # only first 2k to avoid false positives

            timeline.append({
                "role": "observation",
                "msg_index": msg_index,
                "step": step,
                "cmd": "",
                "content": content,
                "viewed_file": None,
                "edited_file": None,
                "is_edit": False,
                "is_test": False,
                "is_search": False,
                "is_revert": False,
                "gt_tags": gt_tags_m,
                "has_gt": bool(gt_tags_m),
                "nudge_delivered": bool(_NUDGE_PATTERN.search(content)),
                "contract_content": contract_text,
                "scope_files": scope_files,
                "has_build_fail": has_build_fail,
            })

    return timeline


def _runtime_post_edits(consumption: dict | None) -> list[dict[str, Any]]:
    """Read timestamped repository mutations from the authoritative runtime ledger."""
    if not isinstance(consumption, dict):
        return []
    path = consumption.get("runtime_ledger_path")
    if not isinstance(path, str) or not os.path.isfile(path):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict) or row.get("event_type") != "post_edit":
                    continue
                file_path = _norm_path(str(row.get("file_path") or ""))
                timestamp = row.get("timestamp_ms")
                if not file_path or not isinstance(timestamp, (int, float)):
                    continue
                key = (int(timestamp), file_path)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"timestamp_ms": int(timestamp), "file_path": file_path})
    except OSError:
        return []
    return sorted(rows, key=lambda row: (row["timestamp_ms"], row["file_path"]))


def _reconcile_authoritative_edits(
    timeline: list[dict], consumption: dict | None,
) -> tuple[list[dict], int]:
    """Enrich command-derived edits with timestamp-joined ``post_edit`` paths.

    The delivery ledger is not a complete successful-write journal: a quiet edit may
    have no GT row at all.  Its rows can therefore add observed paths, but must never
    erase command-derived edits merely because at least one ledger row exists.
    """
    rows = _runtime_post_edits(consumption)
    assistants = [event for event in timeline if event.get("role") == "assistant"]
    timestamped = [event for event in assistants if event.get("timestamp_ms") is not None]
    if not rows or not timestamped:
        return timeline, 0

    joined: list[tuple[dict, str]] = []
    for row in rows:
        homes = [
            event for event in timestamped
            if int(event["timestamp_ms"]) <= row["timestamp_ms"]
        ]
        if homes:
            joined.append((homes[-1], row["file_path"]))
    if not joined:
        return timeline, 0

    seen: set[tuple[int, str]] = {
        (int(event["step"]), str(event["edited_file"]))
        for event in assistants
        if event.get("is_edit") and event.get("edited_file")
    }
    authoritative_seen: set[tuple[int, str]] = set()
    additions: list[dict] = []
    for home, file_path in joined:
        key = (int(home["step"]), file_path)
        if key in authoritative_seen:
            continue
        authoritative_seen.add(key)
        if key in seen:
            home["runtime_post_edit_confirmed"] = True
            continue
        if home.get("edited_file") is None:
            home["edited_file"] = file_path
            home["is_edit"] = True
            home["runtime_post_edit_enriched"] = True
        else:
            synthetic = dict(home)
            synthetic["edited_file"] = file_path
            synthetic["is_edit"] = True
            synthetic["authoritative_edit_synthetic"] = True
            additions.append(synthetic)
        seen.add(key)
    timeline.extend(additions)
    timeline.sort(key=lambda event: (
        int(event.get("msg_index", -1)),
        1 if event.get("authoritative_edit_synthetic") else 0,
    ))
    return timeline, len(authoritative_seen)


# ---------------------------------------------------------------------------
# Section 1: Localization
# ---------------------------------------------------------------------------

def _compute_localization(timeline: list[dict], gold_files: list[str],
                          brief_txt: str, submission: str = "") -> dict:
    gold_set = set(_norm_path(g) for g in gold_files if g)

    # Build ordered lists
    viewed_order: list[str] = []  # all files viewed, in order (with duplicates)
    edited_order: list[str] = []

    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        if ev.get("viewed_file"):
            viewed_order.append(ev["viewed_file"])
        if ev.get("edited_file"):
            edited_order.append(ev["edited_file"])

    unique_viewed = list(dict.fromkeys(viewed_order))
    unique_edited = list(dict.fromkeys(edited_order))
    terminal_step = max(
        (
            int(ev.get("step") or 0)
            for ev in timeline
            if ev.get("role") == "assistant"
        ),
        default=0,
    )

    # G2: suffix-tolerant gold match — the same tolerance the L1-rank match below
    # uses, so a viewed path spelled differently than the normalized gold (repo-
    # relative vs absolute) is not silently counted as "never reached".
    def _is_gold(f: str) -> bool:
        return bool(f) and any(_path_match(f, g) for g in gold_set)

    # files/steps to first gold view. Initialised None (not 0): a run that NEVER
    # views gold must be distinguishable from one that hits gold at step 1 — 0
    # would read as "perfect". Set only inside the found-gold branch.
    files_to_gold_view = None
    steps_to_gold_view = None
    first_gold_view_step = None
    unique_before_gold_view = set()
    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        if ev.get("viewed_file"):
            f = ev["viewed_file"]
            if _is_gold(f):
                if first_gold_view_step is None:
                    first_gold_view_step = ev["step"]
                    files_to_gold_view = len(unique_before_gold_view)
                    steps_to_gold_view = ev["step"] - 1
                break
            unique_before_gold_view.add(f)

    gold_never_reached = first_gold_view_step is None

    # --- EDIT-TRUTH AUTHORITY: submitted patch > command inference ------------------
    # The command-inference edit detector misses edits applied via heredoc / git-apply
    # / multi-file patches and attributes at most ONE file per action, so it reported
    # n_edited=0 on tasks that had SUBMITTED a real source diff (nulling false_file_rate
    # / precision, starving recall / steps_to_gold_edit). When a submission diff is
    # present it is therefore the AUTHORITY for the edited set; command inference
    # (unique_edited) is used ONLY as the fallback when there is no patch.
    cmd_edited_set = set(unique_edited)
    patch_edit_targets = extract_edit_targets(submission)
    if patch_edit_targets:
        raw_edited_set = patch_edit_targets
        edit_authority = "submission_patch"
    else:
        raw_edited_set = cmd_edited_set
        edit_authority = "command_inference"
    # Predeclared, ARM-SYMMETRIC source-only filter (identical rule regardless of GT
    # on/off, task, or repo): gold is source-only (gold_files_from_dataset drops tests),
    # so the edited denominator drops test files and non-source junk on BOTH arms — else
    # a test edit or a .orig/.log artifact would inflate false_file_rate. Excluded
    # counts are reported so the drop is auditable, never silent.
    excluded_test_edits = {f for f in raw_edited_set if _is_test_file(f)}
    _after_test = raw_edited_set - excluded_test_edits
    excluded_junk_edits = {f for f in _after_test if _is_junk_edit_path(f)}
    edited_set = _after_test - excluded_junk_edits
    n_excluded_test_edits = len(excluded_test_edits)
    n_excluded_junk_edits = len(excluded_junk_edits)

    # Gold membership MUST use the same suffix-tolerant _path_match as _is_gold
    # (files_to_gold_edit/view above), _compute_edit_quality, scope, and token —
    # NOT an exact set intersection. A runtime post_edit path normalizes to a
    # './'-prefixed spelling (e.g. './beetsplug/lyrics.py') that exact `&` misses
    # while every other section's _path_match matches, so the exact form under-
    # counted _gold_edited_count to 0 even when gold WAS edited. That split made
    # build_metric_applicability declare edit_attempts_per_gold/first_edit_correctness
    # applicable=False while edit_quality carried a real value -> _metric_state
    # returned "failed" (value present + applicable=False). One matcher, no split.
    gold_edited_set = {f for f in edited_set if any(_path_match(f, g) for g in gold_set)}
    gold_viewed_set = {v for v in unique_viewed if any(_path_match(v, g) for g in gold_set)}
    # SS-PERF (2026-07-19, run 29714439700 llama-factory): RECALL-type numerators count
    # in GOLD space. Two distinct edited spellings suffix-matching ONE gold file (src/
    # layouts) previously made recall = 2/1 = 2.0 — an impossible rate the validity gate
    # correctly refused (metric_structure_valid=false, task not citable). Precision-type
    # numerators stay in the edited/viewed space (numerator and denominator share the
    # same spelling universe, so duplicates cancel and the rate stays bounded).
    gold_files_edited = {
        g for g in gold_set if any(_path_match(f, g) for f in edited_set)}

    # files/steps to first gold edit (fail-open: None until gold is edited). Gated on
    # the edit AUTHORITY so it stays consistent with n_edited: a step counts as a gold
    # edit only when the touched file is one the authority confirms was edited
    # (gold_files_edited), and a pre-gold "wasted" edit only counts when the authority
    # confirms that file too (edited_set) — a transient scratch edit the submitted patch
    # never carried is not a false repo edit. The step number still comes from the
    # timeline; if the edit action was un-attributable (e.g. a git-apply heredoc) the
    # step is honestly None even though the patch proves the file set.
    files_to_gold_edit = None
    steps_to_gold_edit = None
    first_gold_edit_step = None
    unique_before_gold_edit = set()
    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        if ev.get("edited_file"):
            f = ev["edited_file"]
            if any(_path_match(f, g) for g in gold_files_edited):
                if first_gold_edit_step is None:
                    first_gold_edit_step = ev["step"]
                    files_to_gold_edit = len(unique_before_gold_edit)
                    steps_to_gold_edit = ev["step"] - 1
                break
            if any(_path_match(f, e) for e in edited_set):
                unique_before_gold_edit.add(f)

    n_edited = len(edited_set)
    n_gold = len(gold_set)
    n_gold_edited = len(gold_edited_set)
    n_viewed = len(unique_viewed)
    n_gold_viewed = len(gold_viewed_set)

    localization_precision = d8(n_gold_edited / n_edited) if n_edited else None
    # D3 (2026-07-18): no gold denominator -> UNMEASURED (None), never a fabricated 0.0 that
    # reads as a real "0% recall". Mirrors the sibling `else None` metrics above/below.
    localization_recall = d8(len(gold_files_edited) / n_gold) if n_gold else None
    false_file_rate = d8((n_edited - n_gold_edited) / n_edited) if n_edited else None
    exploration_ratio = d8(n_viewed / n_edited) if n_edited else None
    gold_view_precision = d8(n_gold_viewed / n_viewed) if n_viewed else None
    wasted_views = max(0, n_viewed - n_gold_viewed)

    # self_localization_needed: grep/find/rg commands before first gold view
    self_loc = 0
    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        if first_gold_view_step and ev["step"] >= first_gold_view_step:
            break
        if ev.get("is_search"):
            self_loc += 1

    # L1 ranking from brief.txt
    l1_ranked = _parse_l1_ranking(brief_txt)
    gold_rank = 999
    top5 = l1_ranked[:5]
    gold_in_top5 = 0
    for rank, path in enumerate(l1_ranked, 1):
        # Check if any gold file suffix-matches this ranked path
        for g in gold_set:
            if path.endswith(g) or g.endswith(path) or _path_match(path, g):
                if gold_rank == 999:
                    gold_rank = rank
                break
    for path in top5:
        for g in gold_set:
            if path.endswith(g) or g.endswith(path) or _path_match(path, g):
                gold_in_top5 += 1
                break

    gold_in_l1_top_k = gold_rank <= 5
    # D4 (2026-07-18): no gold denominator -> UNMEASURED (None), never a fabricated 0.0.
    navigation_directness = d8(gold_in_top5 / n_gold) if n_gold else None

    return {
        "gold_in_L1_top_k": gold_in_l1_top_k,
        "gold_rank": gold_rank,
        "gold_never_reached": gold_never_reached,
        "first_gold_view_step": first_gold_view_step,
        "files_to_gold_view": files_to_gold_view,
        "steps_to_gold_view": steps_to_gold_view,
        "files_to_gold_edit": files_to_gold_edit,
        "steps_to_gold_edit": steps_to_gold_edit,
        "localization_precision": localization_precision,
        "localization_recall": localization_recall,
        "false_file_rate": false_file_rate,
        "exploration_ratio": exploration_ratio,
        "gold_view_precision": gold_view_precision,
        "wasted_views": wasted_views,
        "navigation_directness": navigation_directness,
        "self_localization_needed": self_loc,
        # internals (useful for debugging)
        "_unique_viewed": n_viewed,
        "_unique_edited": n_edited,
        # SS-PERF: name-faithful gold-space count (gold files edited, bounded by
        # n_gold) — the applicability/censoring consumers read exactly this meaning.
        "_gold_edited_count": len(gold_files_edited),
        "_gold_viewed_count": n_gold_viewed,
        "_terminal_step": terminal_step,
        "_l1_top5": top5,
        # edit-truth provenance + the arm-symmetric source-only filter's drop counts,
        # so the n_edited denominator is auditable (never a silent exclusion).
        "_edit_authority": edit_authority,
        "_patch_edit_count": len(patch_edit_targets),
        "_n_excluded_test_edits": n_excluded_test_edits,
        "_n_excluded_junk_edits": n_excluded_junk_edits,
    }


def _path_match(a: str, b: str) -> bool:
    """True if a and b refer to the same file by suffix match."""
    a, b = a.strip("/"), b.strip("/")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


# ---------------------------------------------------------------------------
# Section 2: Edit Quality
# ---------------------------------------------------------------------------

def _compute_edit_quality(timeline: list[dict], gold_files: list[str],
                           submission: str) -> dict:
    gold_set = set(_norm_path(g) for g in gold_files if g)

    # Per-file edit counts
    edit_counts: dict[str, int] = {}
    # Track which files are edited per step to detect "coherence collapse"
    edit_by_step: list[tuple[int, str]] = []  # (step, file)

    for ev in timeline:
        if ev["role"] != "assistant" or not ev.get("is_edit"):
            continue
        f = ev.get("edited_file") or ""
        if f:
            edit_counts[f] = edit_counts.get(f, 0) + 1
            edit_by_step.append((ev["step"], f))

    gold_edited = {f: c for f, c in edit_counts.items() if any(
        _path_match(f, g) for g in gold_set)}
    n_gold_edited = len(gold_edited)
    total_gold_edits = sum(gold_edited.values())
    edit_attempts_per_gold = d8(total_gold_edits / n_gold_edited) if n_gold_edited else None
    rewrite_count = sum(1 for c in edit_counts.values() if c > 1)

    # Compile failures: observation after an edit that has build-fail markers
    compile_failures_after_edit = 0
    for i, ev in enumerate(timeline):
        if ev["role"] == "assistant" and ev.get("is_edit"):
            # Look at the next observation
            for j in range(i + 1, min(i + 3, len(timeline))):
                if timeline[j]["role"] == "observation" and timeline[j].get("has_build_fail"):
                    compile_failures_after_edit += 1
                    break

    # Revert count
    revert_count = sum(1 for ev in timeline
                       if ev["role"] == "assistant" and ev.get("is_revert"))
    total_edits = sum(edit_counts.values())
    edit_revert_rate = d8(revert_count / total_edits) if total_edits else None

    # first_edit_correctness: per gold file — did the FIRST edit survive to final patch?
    patch_files = set(_norm_path(p) for p in _parse_gold_from_diff(submission or ""))
    first_edit_correctness: dict[str, bool] = {}
    seen_first: set[str] = set()
    for step, f in sorted(edit_by_step, key=lambda x: x[0]):
        if f not in seen_first:
            seen_first.add(f)
            if any(_path_match(f, g) for g in gold_set):
                in_patch = any(_path_match(f, p) for p in patch_files)
                first_edit_correctness[f] = in_patch

    patch_size, patch_file_count = _parse_patch_stats(submission or "")

    return {
        "edit_attempts_per_gold": edit_attempts_per_gold,
        "rewrite_count": rewrite_count,
        "compile_failures_after_edit": compile_failures_after_edit,
        "edit_revert_rate": edit_revert_rate,
        "first_edit_correctness": first_edit_correctness,
        "patch_size": patch_size,
        "patch_files": patch_file_count,
        "_total_edits": total_edits,
        "_revert_count": revert_count,
    }


# ---------------------------------------------------------------------------
# Section 3: Interface Preservation
# ---------------------------------------------------------------------------

def _compute_interface_preservation(timeline: list[dict], consumption: dict | None = None) -> dict:
    """Track byte-proven caller contracts and whether the agent respected them."""
    # Collect all contract warnings delivered: which files got CALLERS warnings?
    warned_files: set[str] = set()  # files for which agent received a CALLERS warning
    contract_obs_indices: list[int] = []  # indices in timeline with contract observations

    native_contracts = _visible_receipts(consumption, "caller_contract")
    if native_contracts or _authoritative_consumption(consumption):
        for entry in native_contracts:
            fp = _norm_path(str(entry.get("file_path") or ""))
            if fp:
                warned_files.add(fp)
            contract_obs_indices.append(int(entry.get("msg_index") or 0))
    else:
        for i, ev in enumerate(timeline):
            if ev["role"] != "observation":
                continue
            contract_text = ev.get("contract_content", "")
            if not contract_text:
                continue
            if _CALLERS_WARNING_PATTERN.search(contract_text):
                # Try to identify the file this contract is about
                # The observation usually precedes an edit to the file
                for j in range(i + 1, min(i + 5, len(timeline))):
                    if timeline[j]["role"] == "assistant" and timeline[j].get("edited_file"):
                        warned_files.add(timeline[j]["edited_file"])
                        break
                contract_obs_indices.append(i)

    n_warnings = len(contract_obs_indices)
    # For each warned file, check if the agent changed the function signature
    # (a heuristic: if they edited that file AND the edit changed the first few lines)
    # Since we cannot diff actual content, we count how many warned files were edited
    signature_changes_warned = 0
    # Conservative: count as a potential violation if the file was edited more than once
    for f in warned_files:
        edits_to_f = sum(1 for ev in timeline
                         if ev["role"] == "assistant" and ev.get("edited_file")
                         and _path_match(ev["edited_file"], f))
        if edits_to_f > 1:
            signature_changes_warned += 1

    # G8: 0 warnings delivered == "not applicable", NOT a perfect score. A layer
    # that never fired must NOT score 1.0 on a MANDATORY metric — emit null.
    contract_compliance_rate = d8(
        (n_warnings - signature_changes_warned) / n_warnings
    ) if n_warnings else None

    return {
        "contract_compliance_rate": contract_compliance_rate,
        "signature_changes_warned": signature_changes_warned,
        "p2p_regression_rate": None,  # requires canonical verifier truth
        "caller_breakage_count": None,  # requires canonical verifier truth
        # DETERMINISM: warned_files is a set; list(set) iteration order is
        # PYTHONHASHSEED-dependent, so the deep-metrics embedder and the standalone
        # performance writer (separate processes) emitted the SAME files in DIFFERENT
        # orders and the byte-exact parity gate failed. sorted() at the producer makes
        # every downstream serialization inherit one canonical order.
        "_warned_files": sorted(warned_files),
        "_contract_warning_count": n_warnings,
    }


def verifier_interface_metrics(truth_data: object) -> dict[str, int | float | None]:
    """Project validated evaluator counts into mandatory interface metrics."""
    result: dict[str, int | float | None] = {
        "p2p_regression_rate": None,
        "caller_breakage_count": None,
    }
    if not isinstance(truth_data, dict):
        return result
    verifier = truth_data.get("verifier_truth")
    if not isinstance(verifier, dict) or verifier.get("schema") != "gt.verifier_truth.v1":
        return result
    if verifier.get("valid") is not True:
        return result
    total = verifier.get("p2p_total")
    failed = verifier.get("p2p_failed")
    if (
        isinstance(total, int) and not isinstance(total, bool) and total > 0
        and isinstance(failed, int) and not isinstance(failed, bool)
        and 0 <= failed <= total
    ):
        result["p2p_regression_rate"] = d8(failed / total)
    # This field is intentionally nullable.  It may be populated only by a
    # future caller-aware verifier join, never inferred from failed test count.
    breakages = verifier.get("caller_breakage_count")
    if isinstance(breakages, int) and not isinstance(breakages, bool) and breakages >= 0:
        result["caller_breakage_count"] = breakages
    return result


def verifier_interface_denominators(truth_data: object) -> dict[str, object]:
    """Objective verifier-truth denominators for interface applicability.

    ``p2p_regression_rate`` and ``caller_breakage_count`` are nullable
    verifier-derived metrics.  A null on either can mean two different things —
    a legitimate not-applicable state (no PASS_TO_PASS denominator; the
    caller-aware join was absent) or a genuine collection failure.  The
    difference is decided by the verifier's OWN machine-readable declarations
    (``valid`` / ``p2p_total`` / ``caller_join_complete``), never by the null
    value itself, so a missing-but-should-exist value can never launder into
    N/A.  These underscore markers are consumed by ``build_metric_applicability``
    and ignored by the mandatory-metric contract.
    """
    markers: dict[str, object] = {
        "_verifier_truth_valid": False,
        "_p2p_total": None,
        "_caller_join_complete": False,
        "_caller_breakage_unmeasured_reason": None,
    }
    if not isinstance(truth_data, dict):
        return markers
    verifier = truth_data.get("verifier_truth")
    if not isinstance(verifier, dict) or verifier.get("schema") != "gt.verifier_truth.v1":
        return markers
    markers["_verifier_truth_valid"] = verifier.get("valid") is True
    total = verifier.get("p2p_total")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        markers["_p2p_total"] = total
    markers["_caller_join_complete"] = verifier.get("caller_join_complete") is True
    reason = verifier.get("caller_breakage_unmeasured_reason")
    if isinstance(reason, str) and reason.strip():
        markers["_caller_breakage_unmeasured_reason"] = reason
    return markers


# ---------------------------------------------------------------------------
# Section 4: Scope Completeness
# ---------------------------------------------------------------------------

def _compute_scope_completeness(timeline: list[dict], gold_files: list[str]) -> dict:
    gold_set = set(_norm_path(g) for g in gold_files if g)

    edited_files: set[str] = set()
    viewed_files: set[str] = set()
    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        if ev.get("edited_file"):
            edited_files.add(ev["edited_file"])
        if ev.get("viewed_file"):
            viewed_files.add(ev["viewed_file"])

    gold_edited = {f for f in edited_files if any(_path_match(f, g) for g in gold_set)}
    non_gold_edited = edited_files - gold_edited
    # SS-PERF (2026-07-19): coverage/discovery count in GOLD space — edited-spelling
    # duplicates suffix-matching one gold file made scope_coverage 2/1 = 2.0 (validity
    # gate refusal) and could fake multi_file_discovery from a single gold file.
    # scope_excess stays in the edited space (spelling-consistent num/denominator).
    gold_files_edited = {
        g for g in gold_set if any(_path_match(f, g) for f in edited_files)}
    n_gold = len(gold_set)
    n_edited = len(edited_files)

    # D5 (2026-07-18): no gold denominator -> UNMEASURED (None), never a fabricated 0.0.
    scope_coverage = d8(len(gold_files_edited) / n_gold) if n_gold else None
    scope_excess = d8(len(non_gold_edited) / n_edited) if n_edited else None

    # multi_file_discovery: for multi-file gold tasks, were the 2nd/3rd files found?
    multi_file_discovery = len(gold_files_edited) >= 2 if n_gold > 1 else None

    # scope_gap_files: gold files the agent NEVER opened
    gold_never_opened = []
    for g in sorted(gold_set):
        viewed = any(_path_match(g, v) for v in viewed_files)
        edited = any(_path_match(g, e) for e in edited_files)
        if not viewed and not edited:
            gold_never_opened.append(g)

    return {
        "scope_coverage": scope_coverage,
        "scope_excess": scope_excess,
        "multi_file_discovery": multi_file_discovery,
        "scope_gap_files": len(gold_never_opened),
        "_gold_gap_list": gold_never_opened,
        # SS-PERF: gold-space count (bounded by n_gold), matching the field's name.
        "_gold_edited_count": len(gold_files_edited),
        "_non_gold_edited_count": len(non_gold_edited),
        "_total_edited_count": n_edited,
    }


# ---------------------------------------------------------------------------
# Section 5: Stuck / Recovery
# ---------------------------------------------------------------------------

def _compute_stuck_recovery(timeline: list[dict], consumption: dict | None = None) -> dict:
    assistant_events = [ev for ev in timeline if ev["role"] == "assistant"]

    # Degenerate loop detection: same cmd repeated >=3 times consecutively
    degenerate_loop_count = 0
    steps_in_loops = 0
    streak = 1
    in_loop = False
    for i in range(1, len(assistant_events)):
        prev = assistant_events[i - 1]["cmd"]
        curr = assistant_events[i]["cmd"]
        # Normalize to avoid cosmetic differences
        if prev.strip()[:120] == curr.strip()[:120]:
            streak += 1
            if streak >= 3 and not in_loop:
                in_loop = True
                degenerate_loop_count += 1
            if streak >= 3:
                steps_in_loops += 1
        else:
            streak = 1
            in_loop = False

    # Coherence collapse: agent rewrites >50% of a file it wrote in the last 5 steps
    # Heuristic: same file edited twice within 5 steps = potential coherence collapse
    coherence_collapse_count = 0
    recent_edits: list[tuple[int, str]] = []  # (step, file)
    for ev in assistant_events:
        if not ev.get("is_edit") or not ev.get("edited_file"):
            continue
        f = ev["edited_file"]
        step = ev["step"]
        # Check if this file was edited in the last 5 steps
        window = [x for x in recent_edits if step - x[0] <= 5]
        if any(_path_match(f, x[1]) for x in window):
            coherence_collapse_count += 1
        recent_edits.append((step, f))

    # stuck_duration: longest streak without edit or test
    stuck = 0
    max_stuck = 0
    for ev in assistant_events:
        if ev.get("is_edit") or ev.get("is_test"):
            max_stuck = max(max_stuck, stuck)
            stuck = 0
        else:
            stuck += 1
    max_stuck = max(max_stuck, stuck)

    # nudge_recovery_steps: steps between a nudge observation and next edit/test
    nudge_recovery_steps_list = []
    native_nudges = _visible_receipts(consumption, "recovery")
    authoritative = _authoritative_consumption(consumption)
    native_by_msg = _receipt_message_indices(native_nudges)
    for i, ev in enumerate(timeline):
        is_native = ev.get("msg_index") in native_by_msg
        if ev["role"] != "observation" or not (
            is_native or (not authoritative and ev.get("nudge_delivered"))
        ):
            continue
        nudge_step = ev["step"]
        # Find next edit or test after this nudge
        for j in range(i + 1, len(timeline)):
            if timeline[j]["role"] == "assistant":
                if timeline[j].get("is_edit") or timeline[j].get("is_test"):
                    recovery = timeline[j]["step"] - nudge_step
                    nudge_recovery_steps_list.append(recovery)
                    break

    nudge_recovery_steps = (
        int(sum(nudge_recovery_steps_list) / len(nudge_recovery_steps_list))
        if nudge_recovery_steps_list else None
    )

    return {
        "degenerate_loop_count": degenerate_loop_count,
        "steps_in_loops": steps_in_loops,
        "nudge_recovery_steps": nudge_recovery_steps,
        "coherence_collapse_count": coherence_collapse_count,
        "stuck_duration": max_stuck,
        "_nudge_recoveries": nudge_recovery_steps_list,
    }


# ---------------------------------------------------------------------------
# Section 6: Verify-before-submit
# ---------------------------------------------------------------------------

def _compute_verify_before_submit(timeline: list[dict], consumption: dict | None = None) -> dict:
    assistant_events = [ev for ev in timeline if ev["role"] == "assistant"]
    n_steps = len(assistant_events)

    test_runs_total = sum(1 for ev in assistant_events if ev.get("is_test"))
    total_edits = sum(1 for ev in assistant_events if ev.get("is_edit"))
    test_edit_ratio = d8(test_runs_total / total_edits) if total_edits else None

    # test_before_submit: any test in last 5 steps before submit
    last_5 = assistant_events[-5:] if len(assistant_events) >= 5 else assistant_events
    test_before_submit = any(ev.get("is_test") for ev in last_5)

    # verify_gap: steps between last edit and next test
    verify_gap = None
    last_edit_step = None
    for ev in assistant_events:
        if ev.get("is_edit"):
            last_edit_step = ev["step"]
    if last_edit_step is not None:
        for ev in assistant_events:
            if ev["step"] > last_edit_step and ev.get("is_test"):
                verify_gap = ev["step"] - last_edit_step
                break
        if verify_gap is None and last_edit_step is not None:
            # no test after last edit
            verify_gap = n_steps - last_edit_step

    # obligation_test_rate: obligations_tested / obligations_edited
    # We look for <gt-obligations> delivery count and tests that follow
    native_obligations = _visible_receipts(consumption, "obligations")
    obligations_delivered = len(native_obligations) if (
        native_obligations or _authoritative_consumption(consumption)
    ) else sum(
        1 for ev in timeline
        if ev["role"] == "observation" and "obligations" in ev.get("gt_tags", [])
    )
    # If obligations were delivered and tests ran, approximate rate. G8: nothing
    # delivered -> null ("not applicable"), never a fabricated 0.0.
    obligation_test_rate = d8(min(test_runs_total, obligations_delivered) / obligations_delivered) \
        if obligations_delivered else None

    return {
        "test_before_submit": test_before_submit,
        "test_runs_total": test_runs_total,
        "test_edit_ratio": test_edit_ratio,
        "obligation_test_rate": obligation_test_rate,
        "verify_gap": verify_gap,
        "_total_edits": total_edits,
        "_n_steps": n_steps,
        "_obligations_delivered": obligations_delivered,
    }


# ---------------------------------------------------------------------------
# Section 8: GT Attribution
# ---------------------------------------------------------------------------

def _compute_gt_attribution(timeline: list[dict], brief_txt: str,
                            consumption: dict | None = None) -> dict:
    l1_top5 = _parse_l1_ranking(brief_txt)[:5]
    l1_top5_set = set(l1_top5)

    # L1_followed_rate: did agent open files GT ranked in top-5?
    viewed_set: set[str] = set()
    for ev in timeline:
        if ev["role"] == "assistant" and ev.get("viewed_file"):
            viewed_set.add(ev["viewed_file"])

    l1_files_opened = sum(
        1 for f in l1_top5_set
        if any(_path_match(f, v) for v in viewed_set)
    )
    l1_followed_rate = d8(l1_files_opened / len(l1_top5)) if l1_top5 else None

    # contract_consulted_rate: did agent view a file's contract BEFORE editing it?
    # Proxy: was a <gt-contract> observation received before the edit step for that file?
    contract_view_steps: dict[str, int] = {}  # file -> last step with contract for it
    native_contracts = _visible_receipts(consumption, "caller_contract")
    if native_contracts or _authoritative_consumption(consumption):
        step_by_msg = {ev.get("msg_index"): ev.get("step", 0) for ev in timeline}
        for entry in native_contracts:
            fp = _norm_path(str(entry.get("file_path") or ""))
            if fp:
                contract_view_steps[fp] = int(step_by_msg.get(entry.get("msg_index"), 0))
    else:
        for i, ev in enumerate(timeline):
            if ev["role"] != "observation" or not ev.get("contract_content"):
                continue
            # Associate with the next edited file
            for j in range(i + 1, min(i + 5, len(timeline))):
                if timeline[j]["role"] == "assistant" and timeline[j].get("edited_file"):
                    f = timeline[j]["edited_file"]
                    contract_view_steps[f] = ev["step"]
                    break

    edited_files: list[tuple[int, str]] = []  # (step, file)
    for ev in timeline:
        if ev["role"] == "assistant" and ev.get("is_edit") and ev.get("edited_file"):
            edited_files.append((ev["step"], ev["edited_file"]))

    edits_with_prior_contract = sum(
        1 for step, f in edited_files
        if any(_path_match(f, cf) and cstep < step
               for cf, cstep in contract_view_steps.items())
    )
    n_edits = len(edited_files)
    contract_consulted_rate = d8(edits_with_prior_contract / n_edits) if n_edits else None

    # obligation_completion_rate: obligations_addressed / obligations_delivered
    native_obligations = _visible_receipts(consumption, "obligations")
    authoritative = _authoritative_consumption(consumption)
    obligations_delivered = len(native_obligations) if (
        native_obligations or authoritative
    ) else sum(
        1 for ev in timeline
        if ev["role"] == "observation" and "obligations" in ev.get("gt_tags", [])
    )
    # We approximate "addressed" as edits that followed an obligation delivery
    # (within 10 steps)
    obligation_followed_edits = 0
    native_obligation_msgs = _receipt_message_indices(native_obligations)
    for i, ev in enumerate(timeline):
        is_native = ev.get("msg_index") in native_obligation_msgs
        if ev["role"] != "observation" or not (
            is_native or (
                not authoritative and "obligations" in ev.get("gt_tags", [])
            )
        ):
            continue
        obs_step = ev["step"]
        for j in range(i + 1, len(timeline)):
            next_ev = timeline[j]
            if next_ev["role"] == "assistant" and next_ev["step"] > obs_step + 10:
                break
            if next_ev["role"] == "assistant" and (
                next_ev.get("is_edit") or next_ev.get("is_test")
            ):
                obligation_followed_edits += 1
                break
    obligation_completion_rate = d8(
        obligation_followed_edits / obligations_delivered
    ) if obligations_delivered else None

    # nudge_action_rate: nudges followed by correct action / nudges
    native_nudges = _visible_receipts(consumption, "recovery")
    nudges_delivered = len(native_nudges) if (
        native_nudges or authoritative
    ) else sum(
        1 for ev in timeline
        if ev["role"] == "observation" and ev.get("nudge_delivered")
    )
    nudge_correct_actions = 0
    native_nudge_msgs = _receipt_message_indices(native_nudges)
    for i, ev in enumerate(timeline):
        is_native = ev.get("msg_index") in native_nudge_msgs
        if ev["role"] != "observation" or not (
            is_native or (not authoritative and ev.get("nudge_delivered"))
        ):
            continue
        for j in range(i + 1, min(i + 3, len(timeline))):
            if timeline[j]["role"] == "assistant":
                if timeline[j].get("is_edit") or timeline[j].get("is_test"):
                    nudge_correct_actions += 1
                break
    # G8: no nudges delivered -> null ("not applicable"), never a fabricated 0.0.
    nudge_action_rate = d8(nudge_correct_actions / nudges_delivered) if nudges_delivered else None

    # scope_chain_followed: did agent open files listed in gt-scope?
    all_scope_files: list[str] = []
    native_scope = _visible_receipts(consumption, "localization")
    if native_scope or authoritative:
        for entry in native_scope:
            fp = _norm_path(str(entry.get("file_path") or ""))
            if fp:
                all_scope_files.append(fp)
            text = str(entry.get("rendered_text") or "")
            all_scope_files.extend(_norm_path(p) for p in re.findall(
                r"([\w./-]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb|php|swift|kt|cs))\b",
                text,
            ))
    else:
        for ev in timeline:
            if ev["role"] == "observation":
                all_scope_files.extend(ev.get("scope_files", []))
    scope_files_set = set(all_scope_files)
    scope_files_opened = sum(
        1 for f in scope_files_set
        if any(_path_match(f, v) for v in viewed_set)
    )
    scope_chain_followed = d8(scope_files_opened / len(scope_files_set)) \
        if scope_files_set else None

    return {
        "L1_followed_rate": l1_followed_rate,
        "contract_consulted_rate": contract_consulted_rate,
        "obligation_completion_rate": obligation_completion_rate,
        "nudge_action_rate": nudge_action_rate,
        "scope_chain_followed": scope_chain_followed,
        "_l1_top5": l1_top5,
        "_l1_count": len(l1_top5),
        "_l1_files_opened": l1_files_opened,
        "_edits_count": n_edits,
        "_obligations_delivered": obligations_delivered,
        "_nudges_delivered": nudges_delivered,
        "_scope_files_count": len(scope_files_set),
    }


# ---------------------------------------------------------------------------
# Section 9: Token Efficiency (localization-dependent subset)
# ---------------------------------------------------------------------------

def _compute_token_efficiency(trajectory: dict, timeline: list[dict],
                               gold_files: list[str],
                               consumption: dict | None = None) -> dict:
    gold_set = set(_norm_path(g) for g in gold_files if g)
    info = trajectory.get("info", {}) or {}
    model_stats = info.get("model_stats", {}) or {}
    usage = extract_trajectory_usage(trajectory)

    total_tokens_in = usage["prompt_tokens"]
    total_tokens_out = usage["completion_tokens"]
    total_steps = int(model_stats.get("api_calls", 0) or 0)
    cache_hit = usage["cache_hit_tokens"]
    cache_miss = usage["cache_miss_tokens"]

    # recorded cost (litellm)
    total_cost_usd = None
    for ck in ("instance_cost", "cost", "total_cost"):
        cv = model_stats.get(ck)
        if isinstance(cv, (int, float)) and cv > 0:
            total_cost_usd = float(cv)
            break

    cache_total = (
        cache_hit + cache_miss
        if cache_hit is not None and cache_miss is not None else None
    )
    cache_hit_rate = d8(cache_hit / cache_total) if cache_total else None

    # gold_files_edited
    gold_edited_set: set[str] = set()
    gold_steps = 0
    non_gold_steps = 0

    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        f = ev.get("edited_file") or ev.get("viewed_file") or ""
        is_gold = bool(f and any(_path_match(f, g) for g in gold_set))
        if ev.get("is_edit") and ev.get("edited_file"):
            if is_gold:
                gold_edited_set.add(ev["edited_file"])
                gold_steps += 1
            else:
                non_gold_steps += 1
        elif ev.get("viewed_file"):
            if is_gold:
                gold_steps += 1
            else:
                non_gold_steps += 1

    n_gold_edited = len(gold_edited_set)
    tokens_per_gold_edit = d8(
        (total_tokens_in + total_tokens_out) / n_gold_edited
    ) if n_gold_edited and total_tokens_in is not None and total_tokens_out is not None else None

    # gt_token_overhead: gt_injected_tokens / total_prompt_tokens
    # GT blocks: chars / 4 as token estimate
    visible = _visible_receipts(consumption)
    gt_observation_chars = sum(int(e.get("chars") or 0) for e in visible) if (
        visible or _authoritative_consumption(consumption)
    ) else sum(
        len(ev.get("content", ""))
        for ev in timeline
        if ev["role"] == "observation" and ev.get("has_gt")
    )
    gt_injected_tokens = gt_observation_chars / 4.0
    gt_token_overhead = d8(gt_injected_tokens / total_tokens_in) if total_tokens_in else None

    # wasted_token_rate (D2, 2026-07-18): the §9 MANDATORY formula is
    # "tokens on non-gold file reads/edits / total" — a TOKEN-attributed ratio. The mini
    # trajectory carries ONLY aggregate usage (no per-step / per-file token attribution), so a
    # true token-based value is NOT computable. We publish the honest STEP PROXY instead —
    # non_gold_steps / (gold_steps + non_gold_steps) — and gt_feature_metrics stamps its
    # formula_provenance as a step proxy so it is never mislabeled as the measured token formula.
    # No steps at all -> None (UNMEASURED); the former reachable `else 0.0` fabricated a real-
    # looking 0% waste on an empty timeline and is removed.
    total_non_idle_steps = gold_steps + non_gold_steps
    wasted_token_rate = d8(non_gold_steps / total_non_idle_steps) \
        if total_non_idle_steps else None

    return {
        "total_steps": total_steps,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "usage_source": usage["source"],
        "usage_records": usage["usage_records"],
        "total_cost_usd": d8(total_cost_usd),
        # This denominator exists only at run scope. Keep the mandatory key in
        # every per-task record, but never fabricate a per-task substitute.
        "cost_per_resolved": None,
        "cost_per_resolved_scope": "run_aggregate",
        "cache_hit_rate": cache_hit_rate,
        "tokens_per_gold_edit": tokens_per_gold_edit,
        "gt_token_overhead": gt_token_overhead,
        "wasted_token_rate": wasted_token_rate,
        # Honest bases for the two token metrics that use ESTIMATES/PROXIES rather than
        # exact per-token attribution (surfaced in each row's formula_provenance downstream):
        #   gt_token_overhead  -> gt_injected_tokens is a chars/4 token ESTIMATE.
        #   wasted_token_rate  -> a STEP proxy (non_gold_steps/non_idle_steps), NOT per-token.
        "_gt_token_overhead_basis": "chars_over_4_token_estimate",
        "_wasted_token_rate_basis": "step_proxy:non_gold_steps/non_idle_steps",
        "_gt_injected_tokens_est": d8(gt_injected_tokens),
        "_n_gold_edited": n_gold_edited,
        "_non_idle_step_count": total_non_idle_steps,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_performance_metrics(
    trajectory_path: str,
    artifacts_dir: str,
    gold_files: list[str] | None = None,
    instance_id: str | None = None,
    gold_jsonl: str | None = None,
    consumption_ledger: dict | None = None,
    verifier_truth: dict | None = None,
) -> dict:
    """Compute all performance metrics from sections 1-6, 8-9.

    Args:
        trajectory_path: Path to mini-swe-agent.trajectory.json
        artifacts_dir:   Directory containing brief.txt, graph.db, etc.
        gold_files:      Explicit gold file paths (operator override). If None, gold is
                         resolved by preference: DATASET gold (see instance_id) ->
                         gold_files.txt on disk -> submission-diff PROXY -> none.
        instance_id:     Task/instance id. With a dataset jsonl (gold_jsonl or the
                         GT_GOLD_JSONL env), resolves TRUE gold from the SWE-bench
                         ``patch`` -> gold_source="dataset_gold" (gold_is_proxy=False).
                         OFFLINE-ONLY: gold lands only in the returned metric record.
        gold_jsonl:      Path to the SWE-bench dataset jsonl; falls back to GT_GOLD_JSONL.

    Returns:
        Dict with all metrics at 8-decimal precision. Never raises.
    """
    out: dict = {
        "schema": "gt_performance_metrics.v1",
        "trajectory_path": trajectory_path or "",
        "artifacts_dir": artifacts_dir or "",
    }
    try:
        trajectory = _safe_load_json(trajectory_path) if trajectory_path else None
        if not isinstance(trajectory, dict):
            out.update({
                "status": "UNMEASURED",
                "reason": "trajectory_unavailable_or_invalid",
            })
            return out

        messages = trajectory.get("messages", []) or []
        info = trajectory.get("info", {}) or {}
        submission = str(info.get("submission") or "")

        # Resolve gold files + record PROVENANCE (G1). "true_gold" = caller-provided
        # or a gold_files.txt on disk; "submission_proxy" = derived from the agent's
        # OWN submission diff (CIRCULAR — measures agreement with what the agent did,
        # even on a FAILED task); "none" = no gold available at all.
        if gold_files is not None:
            gold_source = "true_gold" if gold_files else "none"
        else:
            gold_files = []
            gold_source = "none"
            # PREFERRED: TRUE dataset gold (the SWE-bench `patch`, keyed by instance_id).
            # gold_source="dataset_gold" / gold_is_proxy=False so the timing fields
            # COMPUTE against real gold and can never be confused with the circular
            # submission proxy (HARD LAW — gold is OFFLINE-ONLY, lands only here).
            _jsonl = gold_jsonl or os.environ.get("GT_GOLD_JSONL", "")
            if instance_id and _jsonl:
                dataset_gold = gold_files_from_dataset(instance_id, _jsonl)
                if dataset_gold:
                    gold_files = dataset_gold
                    gold_source = "dataset_gold"
            # Next: gold_files.txt in artifacts_dir (operator-provided true gold)
            if not gold_files:
                gf_path = os.path.join(artifacts_dir or "", "gold_files.txt") if artifacts_dir else ""
                if gf_path and os.path.exists(gf_path):
                    gold_files = _parse_gold_from_file(gf_path)
                    if gold_files:
                        gold_source = "true_gold"
            # Last: submission diff — this is a PROXY, not true gold (nulled below).
            if not gold_files and submission:
                gold_files = _parse_gold_from_diff(submission)
                if gold_files:
                    gold_source = "submission_proxy"

        # Load brief.txt
        brief_txt = ""
        for bname in ("brief.txt", "gt_brief.txt", "pre_task_brief.txt"):
            bp = os.path.join(artifacts_dir or "", bname) if artifacts_dir else ""
            if bp and os.path.exists(bp):
                try:
                    with open(bp, encoding="utf-8", errors="replace") as f:
                        brief_txt = f.read(500_000)
                except OSError:
                    pass
                break

        # Parse timeline
        timeline = _parse_timeline(messages)
        if consumption_ledger is None:
            consumption_ledger = _load_consumption(
                trajectory_path, artifacts_dir, instance_id
            )
        timeline, authoritative_post_edit_count = _reconcile_authoritative_edits(
            timeline, consumption_ledger
        )

        # Sections
        s1 = _compute_localization(timeline, gold_files, brief_txt, submission)
        s2 = _compute_edit_quality(timeline, gold_files, submission)
        s3 = _compute_interface_preservation(timeline, consumption_ledger)
        s3.update(verifier_interface_metrics(verifier_truth))
        s3.update(verifier_interface_denominators(verifier_truth))
        s4 = _compute_scope_completeness(timeline, gold_files)
        s5 = _compute_stuck_recovery(timeline, consumption_ledger)
        s6 = _compute_verify_before_submit(timeline, consumption_ledger)
        s8 = _compute_gt_attribution(timeline, brief_txt, consumption_ledger)
        s9 = _compute_token_efficiency(
            trajectory, timeline, gold_files, consumption_ledger
        )

        # E5 (Section 5 primary endpoint, wired 2026-07-29): "steps to durable
        # correct decision commit". The adjudicator was built + tested but had no
        # caller, so the endpoint could never appear in a published artifact.
        # It imports THIS module's _parse_timeline/_path_match, so the two can
        # never disagree about what an edit is or whether a target is gold.
        # Fail-closed by construction: no gold -> correct=None everywhere and a
        # named reason, never a fabricated 0. Never raises into the caller.
        try:
            from decision_commit_adjudication import adjudicate_decision_commits
            s10 = adjudicate_decision_commits(
                trajectory, gold_files=gold_files or None, gold_source=gold_source,
            )
        except Exception as _e5_exc:  # noqa: BLE001 — an endpoint may not break the grader
            s10 = {
                "schema": "gt.decision_commit_adjudication.v1",
                "decisions": [],
                "summary": {
                    "steps_to_first_durable_correct_commit": None,
                    "reason": f"adjudicator_unavailable:{type(_e5_exc).__name__}",
                    "n_commits": 0, "n_correct": None, "n_durable": 0,
                    "gold_source": gold_source,
                },
            }

        out.update({
            "localization": s1,
            "edit_quality": s2,
            "decision_commit": s10,
            "interface_preservation": s3,
            "scope_completeness": s4,
            "stuck_recovery": s5,
            "verify_before_submit": s6,
            "gt_attribution": s8,
            "token_efficiency": s9,
            "gold_files_used": gold_files,
            "gold_source": gold_source,
            "gold_is_proxy": gold_source == "submission_proxy",
            "n_messages": len(messages),
            "n_gold_files": len(gold_files),
            "brief_found": bool(brief_txt),
            "submission_found": bool(submission),
            "terminal_status": str(info.get("exit_status") or ""),
            "consumption_ledger_schema": (
                consumption_ledger.get("schema")
                if isinstance(consumption_ledger, dict) else None
            ),
            "native_delivery_rows_joined": (
                consumption_ledger.get("ledger_rows_joined")
                if isinstance(consumption_ledger, dict) else None
            ),
            "edit_path_source": (
                "command_plus_runtime_post_edit" if authoritative_post_edit_count
                else "command_fallback"
            ),
            "authoritative_post_edit_count": authoritative_post_edit_count,
        })

        # G1 + MANDATORY_METRICS persistence rule 6: a submission-proxy "gold"
        # is circular, and no gold at all has no denominator. In both cases every
        # mandatory localization metric plus the other gold-dependent fields is
        # N/A, never a fabricated false/999/0 score.
        if gold_source in {"submission_proxy", "none"}:
            _null_gold_derived(out)
        out["metric_applicability"] = build_metric_applicability(out)
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        out["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        import traceback
        out["traceback"] = traceback.format_exc()[:1000]

    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _opt(flag: str) -> str:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


def main() -> int:
    positionals = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(positionals) < 2:
        print(
            "usage: gt_performance_metrics.py <trajectory.json> <artifacts_dir> "
            "[--gold gold_files.txt] [--out metrics.json]"
        )
        return 2

    trajectory_path = positionals[0]
    artifacts_dir = positionals[1]
    gold_flag = _opt("--gold")
    instance_id = _opt("--instance-id") or None
    gold_jsonl = _opt("--gold-jsonl") or None
    task_truth_path = _opt("--task-truth")
    out_path = _opt("--out") or os.path.join(
        artifacts_dir, "gt_performance_metrics.json"
    )

    gold_files: list[str] | None = None
    if gold_flag:
        gold_files = _parse_gold_from_file(gold_flag)
        if not gold_files:
            print(f"[WARN] --gold {gold_flag!r} produced no files", file=sys.stderr)

    result = compute_performance_metrics(
        trajectory_path, artifacts_dir, gold_files,
        instance_id=instance_id, gold_jsonl=gold_jsonl,
        verifier_truth=(
            _safe_load_json(task_truth_path) if task_truth_path else None
        ),
    )

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"[GT_PERF] wrote {out_path}")
    except OSError as exc:
        print(f"[GT_PERF] FAILED to write {out_path}: {exc}", file=sys.stderr)
        return 1

    # Print summary
    loc = result.get("localization", {})
    s4 = result.get("scope_completeness", {})
    s6 = result.get("verify_before_submit", {})
    s8 = result.get("gt_attribution", {})

    def _f4(v):  # None-tolerant: a null metric prints as "null", never crashes / fakes 0
        return f"{v:.4f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "null"

    print(
        f"[GT_PERF] gold_rank={loc.get('gold_rank')} "
        f"steps_to_gold_view={loc.get('steps_to_gold_view')} "
        f"localization_precision={_f4(loc.get('localization_precision'))} "
        f"scope_coverage={_f4(s4.get('scope_coverage'))} "
        f"test_before_submit={s6.get('test_before_submit')} "
        f"L1_followed_rate={_f4(s8.get('L1_followed_rate'))}"
    )
    if result.get("error"):
        print(f"[GT_PERF] ERROR: {result['error']}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Inline tests (run when invoked directly with no args)
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    """Smoke test with a small synthetic trajectory fixture."""
    print("Running gt_performance_metrics inline tests...")

    # Minimal trajectory fixture
    fixture = {
        "messages": [
            # Step 1: read a non-gold file
            {"role": "assistant", "content": "I'll look at the code", "tool_calls": [
                {"function": {"arguments": json.dumps({"command": "view", "path": "src/utils.py"})}}
            ]},
            {"role": "tool", "content": "def helper(): pass"},
            # Step 2: search
            {"role": "assistant", "content": "Let me search", "tool_calls": [
                {"function": {"arguments": json.dumps({"command": "grep -r 'get_user' src/"})}}
            ]},
            {"role": "tool", "content": "src/auth.py:def get_user(id):"},
            # Step 3: read gold file
            {"role": "assistant", "content": "Found it", "tool_calls": [
                {"function": {"arguments": json.dumps({"command": "view", "path": "src/auth.py"})}}
            ]},
            {"role": "tool", "content": "<gt-contract>def get_user(id) -> User: [CALLERS] 3 callers</gt-contract>"},
            # Step 4: edit gold file
            {"role": "assistant", "content": "Making the fix", "tool_calls": [
                {"function": {"arguments": json.dumps({
                    "command": "str_replace",
                    "path": "src/auth.py",
                    "old_str": "return None",
                    "new_str": "return db.get(id)"
                })}}
            ]},
            {"role": "tool", "content": "File updated."},
            # Step 5: run tests
            {"role": "assistant", "content": "Running tests", "tool_calls": [
                {"function": {"arguments": json.dumps({"command": "pytest tests/"})}}
            ]},
            {"role": "tool", "content": "5 passed"},
        ],
        "info": {
            "submission": "diff --git a/src/auth.py b/src/auth.py\n+return db.get(id)\n-return None",
            "exit_status": "submitted",
            "model_stats": {
                "api_calls": 5,
                "prompt_tokens": 10000,
                "completion_tokens": 2000,
                "instance_cost": 0.0025,
            },
        },
    }

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write fixture
        traj_path = os.path.join(tmpdir, "mini-swe-agent.trajectory.json")
        with open(traj_path, "w") as f:
            json.dump(fixture, f)

        # Write a brief.txt
        brief = "1. src/auth.py\n2. src/utils.py\n"
        with open(os.path.join(tmpdir, "brief.txt"), "w") as f:
            f.write(brief)

        result = compute_performance_metrics(traj_path, tmpdir, ["src/auth.py"])

    assert "localization" in result, "Missing localization section"
    assert "edit_quality" in result, "Missing edit_quality section"
    assert "scope_completeness" in result, "Missing scope_completeness"
    assert "verify_before_submit" in result, "Missing verify_before_submit"

    loc = result["localization"]
    assert loc["gold_rank"] == 1, f"Expected gold_rank=1, got {loc['gold_rank']}"
    assert loc["files_to_gold_view"] == 1, f"Expected 1 file before gold view, got {loc['files_to_gold_view']}"
    assert loc["self_localization_needed"] == 1, f"Expected 1 search cmd, got {loc['self_localization_needed']}"
    assert loc["localization_precision"] == 1.0, f"Expected precision=1.0, got {loc['localization_precision']}"

    vbs_pre = result["verify_before_submit"]
    assert vbs_pre["test_runs_total"] == 1, \
        f"Test detection failed: expected 1, got {vbs_pre['test_runs_total']}"

    vbs = result["verify_before_submit"]
    assert vbs["test_before_submit"] is True, "Expected test_before_submit=True"
    assert vbs["test_runs_total"] == 1, f"Expected 1 test run, got {vbs['test_runs_total']}"

    sc = result["scope_completeness"]
    assert sc["scope_coverage"] == 1.0, f"Expected scope_coverage=1.0, got {sc['scope_coverage']}"

    s8 = result["gt_attribution"]
    assert s8["L1_followed_rate"] == 1.0, f"Expected L1_followed_rate=1.0, got {s8['L1_followed_rate']}"

    assert result.get("error") is None, f"Unexpected error: {result.get('error')}"
    print("All inline tests passed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # No args: run inline tests
        _run_tests()
    else:
        raise SystemExit(main())
