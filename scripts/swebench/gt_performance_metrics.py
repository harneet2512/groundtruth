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
import math
import os
import re
import sys
from typing import Any

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


def _decode_tool_args(tool_calls_json: str) -> dict:
    """Attempt to parse the structured arguments from a tool_calls JSON dump.

    mini-swe-agent stores: tool_calls = [{"function": {"arguments": "<json-string>"}}]
    The outer JSON.dumps() escapes the inner JSON, so we need two decode levels.
    Returns the first decoded arguments dict, or {} on any failure.
    """
    if not tool_calls_json:
        return {}
    try:
        tc = json.loads(tool_calls_json)
        if not isinstance(tc, list) or not tc:
            return {}
        args_raw = tc[0].get("function", {}).get("arguments", "")
        if isinstance(args_raw, dict):
            return args_raw
        if isinstance(args_raw, str):
            return json.loads(args_raw)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    return {}


_VIEW_VERBS = {"view", "open", "read", "show", "display"}
_EDIT_VERBS_SET = {"str_replace", "create", "insert", "write", "overwrite", "edit", "modify"}
_NON_EDIT_VERBS_SET = {"view", "open", "read", "undo_edit", "undo", "show", "display"}


def _extract_viewed_file(tool_calls_json: str, full_cmd: str) -> str | None:
    """Extract a file path from a read command.

    Tries structured args first (tool_calls JSON), then regex on the full text.
    Returns None if not found.
    """
    # 1. Structured parse of tool_calls
    args = _decode_tool_args(tool_calls_json)
    if args:
        verb = str(args.get("command") or "").lower()
        path = str(args.get("path") or args.get("file_path") or "")
        if path and verb in _VIEW_VERBS:
            return _norm_path(path)
        if path and verb not in _EDIT_VERBS_SET and verb != "":
            # command might be the shell command itself (bash agent)
            # or "view" already captured above — only return for explicit view verbs
            pass
        # Some agents use just {"command": "cat src/foo.py"} as a bash call
        bash_cmd = str(args.get("command") or "")
        if bash_cmd:
            m = re.search(r"(?:^|\s)(?:cat|head|tail|bat|less|more)\s+(?:-[^\s]+\s+)*([^\s|;&<>\"']+)", bash_cmd, re.I)
            if m:
                p = m.group(1).strip()
                if "/" in p or "." in p:
                    return _norm_path(p)

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

    # Shell command fallback
    m = re.search(r"(?:^|\s)(?:cat|head|tail|bat|less|more|sed\s+-n)\s+(?:-[^\s]+\s+)*([^\s|;&<>\"']+)", full_cmd, re.I)
    if m:
        p = m.group(1).strip()
        if "/" in p or "." in p:
            return _norm_path(p)
    return None


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

    # Shell patterns
    m = re.search(r"sed\s+-i\s+[^\s]+\s+([^\s|;&]+)", full_cmd, re.I)
    if m:
        return _norm_path(m.group(1))
    m = re.search(r"tee\s+([^\s|;&<]+)\s*<<", full_cmd, re.I)
    if m:
        return _norm_path(m.group(1))
    m = re.search(r"cat\s*>\s*([^\s|;&<\"']+)", full_cmd, re.I)
    if m:
        return _norm_path(m.group(1))
    m = re.search(r"patch\s+(?:-[^\s]+\s+)*([^\s|;&]+\.(?:py|go|ts|js|java|rs|cpp|c|h|rb))", full_cmd, re.I)
    if m:
        return _norm_path(m.group(1))
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
        # bash command might contain edit patterns
        bash_cmd = str(args.get("command") or "")
        if bash_cmd:
            if re.search(r"sed\s+-i|tee\s+.*<<|cat\s*>|patch\s|apply_patch", bash_cmd, re.I):
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
    # Shell patterns
    return bool(re.search(r"sed\s+-i|tee\s+.*<<|cat\s*>|patch\s|apply_patch", full_cmd, re.I))


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
        "false_file_rate", "gold_view_precision", "wasted_views", "navigation_directness",
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

    for m in messages:
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
                "step": step,
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


# ---------------------------------------------------------------------------
# Section 1: Localization
# ---------------------------------------------------------------------------

def _compute_localization(timeline: list[dict], gold_files: list[str],
                          brief_txt: str) -> dict:
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

    # files/steps to first gold edit (same fail-open fix: None until gold edited).
    files_to_gold_edit = None
    steps_to_gold_edit = None
    first_gold_edit_step = None
    unique_before_gold_edit = set()
    for ev in timeline:
        if ev["role"] != "assistant":
            continue
        if ev.get("edited_file"):
            f = ev["edited_file"]
            if _is_gold(f):
                if first_gold_edit_step is None:
                    first_gold_edit_step = ev["step"]
                    files_to_gold_edit = len(unique_before_gold_edit)
                    steps_to_gold_edit = ev["step"] - 1
                break
            unique_before_gold_edit.add(f)

    gold_never_reached = first_gold_view_step is None

    edited_set = set(unique_edited)
    gold_edited_set = edited_set & gold_set
    gold_viewed_set = set(unique_viewed) & gold_set

    n_edited = len(edited_set)
    n_gold = len(gold_set)
    n_gold_edited = len(gold_edited_set)
    n_viewed = len(unique_viewed)
    n_gold_viewed = len(gold_viewed_set)

    localization_precision = d8(n_gold_edited / n_edited) if n_edited else 0.0
    localization_recall = d8(n_gold_edited / n_gold) if n_gold else 0.0
    false_file_rate = d8((n_edited - n_gold_edited) / n_edited) if n_edited else 0.0
    exploration_ratio = d8(n_viewed / n_edited) if n_edited else d8(n_viewed)
    gold_view_precision = d8(n_gold_viewed / n_viewed) if n_viewed else 0.0
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
    navigation_directness = d8(gold_in_top5 / n_gold) if n_gold else 0.0

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
        "_gold_edited_count": n_gold_edited,
        "_gold_viewed_count": n_gold_viewed,
        "_l1_top5": top5,
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
    edit_attempts_per_gold = d8(total_gold_edits / n_gold_edited) if n_gold_edited else 0.0
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
    edit_revert_rate = d8(revert_count / total_edits) if total_edits else 0.0

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

def _compute_interface_preservation(timeline: list[dict]) -> dict:
    """Track <gt-contract> [CALLERS] warnings and whether the agent respected them."""
    # Collect all contract warnings delivered: which files got CALLERS warnings?
    warned_files: set[str] = set()  # files for which agent received a CALLERS warning
    contract_obs_indices: list[int] = []  # indices in timeline with contract observations

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
        "p2p_regression_rate": 0.0,  # requires verifier — filled by gt_deep_metrics
        "caller_breakage_count": 0,  # requires verifier
        "_warned_files": list(warned_files),
        "_contract_warning_count": n_warnings,
    }


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
    n_gold = len(gold_set)
    n_edited = len(edited_files)

    scope_coverage = d8(len(gold_edited) / n_gold) if n_gold else 0.0
    scope_excess = d8(len(non_gold_edited) / n_edited) if n_edited else 0.0

    # multi_file_discovery: for multi-file gold tasks, were the 2nd/3rd files found?
    multi_file_discovery = False
    if n_gold > 1:
        multi_file_discovery = len(gold_edited) >= 2

    # scope_gap_files: gold files the agent NEVER opened
    gold_never_opened = []
    for g in gold_set:
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
        "_gold_edited_count": len(gold_edited),
        "_non_gold_edited_count": len(non_gold_edited),
    }


# ---------------------------------------------------------------------------
# Section 5: Stuck / Recovery
# ---------------------------------------------------------------------------

def _compute_stuck_recovery(timeline: list[dict]) -> dict:
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
    for i, ev in enumerate(timeline):
        if ev["role"] != "observation" or not ev.get("nudge_delivered"):
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
        if nudge_recovery_steps_list else 0
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

def _compute_verify_before_submit(timeline: list[dict]) -> dict:
    assistant_events = [ev for ev in timeline if ev["role"] == "assistant"]
    n_steps = len(assistant_events)

    test_runs_total = sum(1 for ev in assistant_events if ev.get("is_test"))
    total_edits = sum(1 for ev in assistant_events if ev.get("is_edit"))
    test_edit_ratio = d8(test_runs_total / total_edits) if total_edits else 0.0

    # test_before_submit: any test in last 5 steps before submit
    last_5 = assistant_events[-5:] if len(assistant_events) >= 5 else assistant_events
    test_before_submit = any(ev.get("is_test") for ev in last_5)

    # verify_gap: steps between last edit and next test
    verify_gap = 0
    last_edit_step = None
    for ev in assistant_events:
        if ev.get("is_edit"):
            last_edit_step = ev["step"]
    if last_edit_step is not None:
        for ev in assistant_events:
            if ev["step"] > last_edit_step and ev.get("is_test"):
                verify_gap = ev["step"] - last_edit_step
                break
        if verify_gap == 0 and last_edit_step:
            # no test after last edit
            verify_gap = n_steps - last_edit_step

    # obligation_test_rate: obligations_tested / obligations_edited
    # We look for <gt-obligations> delivery count and tests that follow
    obligations_delivered = sum(
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
    }


# ---------------------------------------------------------------------------
# Section 8: GT Attribution
# ---------------------------------------------------------------------------

def _compute_gt_attribution(timeline: list[dict], brief_txt: str) -> dict:
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
    l1_followed_rate = d8(l1_files_opened / len(l1_top5)) if l1_top5 else 0.0

    # contract_consulted_rate: did agent view a file's contract BEFORE editing it?
    # Proxy: was a <gt-contract> observation received before the edit step for that file?
    contract_view_steps: dict[str, int] = {}  # file -> last step with contract for it
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
        if f in contract_view_steps and contract_view_steps[f] < step
    )
    n_edits = len(edited_files)
    contract_consulted_rate = d8(edits_with_prior_contract / n_edits) if n_edits else 0.0

    # obligation_completion_rate: obligations_addressed / obligations_delivered
    obligations_delivered = sum(
        1 for ev in timeline
        if ev["role"] == "observation" and "obligations" in ev.get("gt_tags", [])
    )
    # We approximate "addressed" as edits that followed an obligation delivery
    # (within 10 steps)
    obligation_followed_edits = 0
    for i, ev in enumerate(timeline):
        if ev["role"] != "observation" or "obligations" not in ev.get("gt_tags", []):
            continue
        obs_step = ev["step"]
        for j in range(i + 1, len(timeline)):
            next_ev = timeline[j]
            if next_ev["role"] == "assistant" and next_ev["step"] > obs_step + 10:
                break
            if next_ev["role"] == "assistant" and next_ev.get("is_edit"):
                obligation_followed_edits += 1
                break
    obligation_completion_rate = d8(
        obligation_followed_edits / obligations_delivered
    ) if obligations_delivered else 0.0

    # nudge_action_rate: nudges followed by correct action / nudges
    nudges_delivered = sum(
        1 for ev in timeline
        if ev["role"] == "observation" and ev.get("nudge_delivered")
    )
    nudge_correct_actions = 0
    for i, ev in enumerate(timeline):
        if ev["role"] != "observation" or not ev.get("nudge_delivered"):
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
    for ev in timeline:
        if ev["role"] == "observation":
            all_scope_files.extend(ev.get("scope_files", []))
    scope_files_set = set(all_scope_files)
    scope_files_opened = sum(
        1 for f in scope_files_set
        if any(_path_match(f, v) for v in viewed_set)
    )
    scope_chain_followed = d8(scope_files_opened / len(scope_files_set)) \
        if scope_files_set else 0.0

    return {
        "L1_followed_rate": l1_followed_rate,
        "contract_consulted_rate": contract_consulted_rate,
        "obligation_completion_rate": obligation_completion_rate,
        "nudge_action_rate": nudge_action_rate,
        "scope_chain_followed": scope_chain_followed,
        "_l1_top5": l1_top5,
        "_l1_files_opened": l1_files_opened,
        "_nudges_delivered": nudges_delivered,
        "_scope_files_count": len(scope_files_set),
    }


# ---------------------------------------------------------------------------
# Section 9: Token Efficiency (localization-dependent subset)
# ---------------------------------------------------------------------------

def _compute_token_efficiency(trajectory: dict, timeline: list[dict],
                               gold_files: list[str]) -> dict:
    gold_set = set(_norm_path(g) for g in gold_files if g)
    info = trajectory.get("info", {}) or {}
    model_stats = info.get("model_stats", {}) or {}

    total_tokens_in = int(model_stats.get("prompt_tokens", 0) or 0)
    total_tokens_out = int(model_stats.get("completion_tokens", 0) or 0)
    total_steps = int(model_stats.get("api_calls", 0) or 0)
    cache_hit = int(model_stats.get("prompt_cache_hit_tokens", 0) or 0)
    cache_miss = int(model_stats.get("prompt_cache_miss_tokens", 0) or 0)

    # recorded cost (litellm)
    total_cost_usd = 0.0
    for ck in ("instance_cost", "cost", "total_cost"):
        cv = model_stats.get(ck)
        if isinstance(cv, (int, float)) and cv > 0:
            total_cost_usd = float(cv)
            break

    cache_total = cache_hit + cache_miss
    cache_hit_rate = d8(cache_hit / cache_total) if cache_total else 0.0

    # gold_files_edited
    gold_edited_set: set[str] = set()
    non_gold_tokens = 0  # proxy: steps spent on non-gold files
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
    ) if n_gold_edited else 0.0

    # gt_token_overhead: gt_injected_tokens / total_prompt_tokens
    # GT blocks: chars / 4 as token estimate
    gt_observation_chars = sum(
        len(ev.get("content", ""))
        for ev in timeline
        if ev["role"] == "observation" and ev.get("has_gt")
    )
    gt_injected_tokens = gt_observation_chars / 4.0
    gt_token_overhead = d8(gt_injected_tokens / total_tokens_in) if total_tokens_in else 0.0

    # wasted_token_rate: tokens on non-gold file reads/edits / total
    total_non_idle_steps = gold_steps + non_gold_steps
    wasted_token_rate = d8(non_gold_steps / total_non_idle_steps) \
        if total_non_idle_steps else 0.0

    return {
        "total_steps": total_steps,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_cost_usd": d8(total_cost_usd),
        "cache_hit_rate": cache_hit_rate,
        "tokens_per_gold_edit": tokens_per_gold_edit,
        "gt_token_overhead": gt_token_overhead,
        "wasted_token_rate": wasted_token_rate,
        "_gt_injected_tokens_est": d8(gt_injected_tokens),
        "_n_gold_edited": n_gold_edited,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_performance_metrics(
    trajectory_path: str,
    artifacts_dir: str,
    gold_files: list[str] | None = None,
) -> dict:
    """Compute all performance metrics from sections 1-6, 8-9.

    Args:
        trajectory_path: Path to mini-swe-agent.trajectory.json
        artifacts_dir:   Directory containing brief.txt, graph.db, etc.
        gold_files:      List of gold file paths. If None, derived from submission diff
                         or a gold_files.txt in artifacts_dir.

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
            trajectory = {}

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
            # Try gold_files.txt in artifacts_dir
            gf_path = os.path.join(artifacts_dir or "", "gold_files.txt") if artifacts_dir else ""
            if gf_path and os.path.exists(gf_path):
                gold_files = _parse_gold_from_file(gf_path)
                if gold_files:
                    gold_source = "true_gold"
            # Fall back to submission diff — this is a PROXY, not true gold.
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

        # Sections
        s1 = _compute_localization(timeline, gold_files, brief_txt)
        s2 = _compute_edit_quality(timeline, gold_files, submission)
        s3 = _compute_interface_preservation(timeline)
        s4 = _compute_scope_completeness(timeline, gold_files)
        s5 = _compute_stuck_recovery(timeline)
        s6 = _compute_verify_before_submit(timeline)
        s8 = _compute_gt_attribution(timeline, brief_txt)
        s9 = _compute_token_efficiency(trajectory, timeline, gold_files)

        out.update({
            "localization": s1,
            "edit_quality": s2,
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
        })

        # G1: under a submission-proxy "gold" every gold-derived metric is circular
        # (it measures agreement with the agent's OWN patch, even on a failed task).
        # Emit them as null so no fabricated score is read as real.
        if gold_source == "submission_proxy":
            _null_gold_derived(out)
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
    out_path = _opt("--out") or os.path.join(
        artifacts_dir, "gt_performance_metrics.json"
    )

    gold_files: list[str] | None = None
    if gold_flag:
        gold_files = _parse_gold_from_file(gold_flag)
        if not gold_files:
            print(f"[WARN] --gold {gold_flag!r} produced no files", file=sys.stderr)

    result = compute_performance_metrics(trajectory_path, artifacts_dir, gold_files)

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
