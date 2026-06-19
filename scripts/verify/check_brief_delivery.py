#!/usr/bin/env python3
"""check_brief_delivery.py — prove the L1 GT brief was DELIVERED to the agent.

The only acceptable delivery proof: the AGENT-FACING first-turn instruction inside
a real output.jsonl contains the expected non-empty GT content. This script does
NOT look at gt_brief_full, producer logs, or any non-agent-facing field — only the
instruction the agent actually received.

Asserts on that instruction:
  - exactly one <gt-task-brief> open tag and one </gt-task-brief> close tag
  - if <gt-graph-map> is present, its body (whitespace-stripped) length > 0
  - --require-graph-map : <gt-graph-map> must be present AND non-empty
  - no hidden [GT_*] diagnostic leakage ([GT_META]/[GT_BRIEF_DIAG]/[GT_RANK_DIAG]/...)
  - --require-contract-line : a 'Contract:' line must be present

Opt-in gates (default off so existing callers are unaffected; always COMPUTED,
only FAIL under the flag):

  --require-balanced-contracts (C1 regression guard)
      For every agent-facing instruction line whose stripped form starts with
      'Contract:' / 'Preserve:' or contains 'preserve ' / 'guard_clause:',
      extract the guard value(s) and assert each is well-formed: balanced
      quotes/brackets, no unterminated string literal, no trailing dangling
      binary/boolean operator. This catches the C1 failure where a guard value
      was clipped mid-token (e.g. ending in
      `raise TypeError("DocumentSplitter expects a List of Document` or
      `(documents and not`). The check first tries to import
      groundtruth.runtime.sanitizer.is_well_formed_clause; if that import fails
      (standalone run, no PYTHONPATH), it falls back to an inline balance check.

      ALSO scans agent OBSERVATION `content` (history records, not just the
      instruction) for guard-bearing lines — any line carrying
      'PRESERVE:' / '[RAISES]' / '[CATCHES]' / 'SEMANTIC WARNING:' /
      'guard_clause:' / 'preserve ' — and applies the SAME well-formedness
      contract to each extracted value. This catches the C1 truncation that
      lived in a POST-EDIT L3/L3b observation (the haystack run), which the
      instruction-only scan missed. Malformed observation guards are reported in
      `malformed_observation_guards`; both sides FAIL under this flag.

  --require-layer-markers
      Assert post-localization GT layer markers in the agent's OBSERVATION
      `content` (history records only — NOT the instruction, NOT telemetry
      fields like gt_layer_event). Each marker is gated on its own trigger:
        - L3  `<gt-evidence` block or `[GT] Post-edit:` : required iff an edit occurred.
        - L3b [CONTRACT] line       : required iff an edit action occurred.
        - L6  '[GT_VERIFY] Tests covering' : required iff an edit->review
              transition exists (an edit action followed later by a
              review/submit/finish action).
      A layer with NO trigger is NOT a failure — we only FAIL when the trigger
      is present but the marker is absent. This avoids penalising runs that
      legitimately never reached the layer's firing condition.

JSONL-parsed, never grep. Exit 0 on PASS, nonzero on FAIL.

Usage:
  python scripts/verify/check_brief_delivery.py <output.jsonl> [--json]
      [--require-graph-map] [--require-contract-line] [--allow-empty-graph-map]
      [--require-balanced-contracts] [--require-layer-markers]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Hidden diagnostics that must be filtered to stderr and NEVER reach the agent.
# (Mirrors oh_gt_full_wrapper _HIDDEN_PREFIXES + the brief-runner diag prints.)
HIDDEN_DIAG_MARKERS = [
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]", "[GT_DELIVERY]",
    "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]", "[GT_SUMMARY]",
    "[GT_BRIEF_DIAG]", "[GT_RANK_DIAG]", "[GT_BRIEF_FAILED]", "[GT_BRIEF_TRACEBACK]",
]

DEAD_PATH_MARKERS = [
    "<gt-v22-brief",
    "v22 brief:",
    "v22_brief",
    "old graph_map",
]

GT_OBSERVATION_MARKERS = (
    "<gt-evidence",
    "[GT] Post-edit:",
    "<gt-context",
    "[GT] ",
    "[GT_VERIFY]",
)


def extract_first_turn_instruction(path: Path) -> str:
    """Return the agent-facing first-turn instruction (the brief-bearing user input).

    Looks ONLY at the top-level `instruction` field and history `content`/`message`
    — never `gt_brief`/`gt_brief_full` (the logging copies). Returns "" if none found.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            instr = rec.get("instruction")
            if isinstance(instr, str) and "<gt-task-brief>" in instr:
                return instr
            hist = rec.get("history")
            if isinstance(hist, list):
                for e in hist:
                    if isinstance(e, dict):
                        c = e.get("content") or e.get("message") or ""
                        if isinstance(c, str) and "<gt-task-brief>" in c:
                            return c
    return ""


def _graph_map_body(instr: str) -> tuple[bool, int]:
    m = re.search(r"<gt-graph-map>(.*?)</gt-graph-map>", instr, re.S)
    if not m:
        return (False, 0)
    return (True, len(m.group(1).strip()))


# --------------------------------------------------------------------------- #
# C1 regression guard: balanced contract-clause well-formedness                #
# --------------------------------------------------------------------------- #

# Try the canonical implementation first. When run standalone (no PYTHONPATH to
# the package) the import fails and we fall back to the inline check below, which
# is intentionally a strict subset of the same contract.
try:  # pragma: no cover - exercised by both branches across run modes
    from groundtruth.runtime.sanitizer import is_well_formed_clause as _is_well_formed_clause  # type: ignore
    _USING_SANITIZER_CLAUSE = True
except Exception:  # ImportError or any package-load failure -> inline fallback
    _is_well_formed_clause = None  # type: ignore[assignment]
    _USING_SANITIZER_CLAUSE = False


# Tokens that must never be the LAST token of a guard value (a guard clipped
# mid-expression leaves a dangling binary/boolean operator).
_DANGLING_TRAILERS = frozenset({
    "and", "or", "not", "in", "is", "if", "else",
    "+", "-", "*", "/", "%", "**", "//",
    "==", "!=", "<", ">", "<=", ">=", "=",
    "&", "|", "^", "~", "<<", ">>",
    ",", ".", "->", ":", "(", "[", "{",
})


def _inline_well_formed_clause(value: str) -> bool:
    """Strict inline balance check (fallback when the sanitizer import fails).

    Returns False when the clause is shred mid-token: unbalanced brackets, an
    unterminated string literal, or a trailing dangling binary/boolean operator.
    Quote-aware so brackets inside string literals are not counted.
    """
    s = value.strip()
    if not s:
        return True  # an empty guard is vacuously well-formed (nothing to break)

    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set(pairs.values())
    quote: str | None = None
    escaped = False
    for ch in s:
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in opens:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False  # unbalanced / mismatched closer
            stack.pop()
    if quote is not None:
        return False  # unterminated string literal
    if stack:
        return False  # unclosed bracket

    # Trailing dangling operator: look at the final whitespace-delimited token,
    # and at the final non-space character for glued operators (e.g. "x +").
    tokens = s.split()
    last = tokens[-1] if tokens else ""
    if last in _DANGLING_TRAILERS:
        return False
    if s[-1] in {"+", "-", "*", "/", "%", "&", "|", "^", "<", ">", "=", ",", "."}:
        return False
    return True


def _clause_is_well_formed(value: str) -> bool:
    """Dispatch to the sanitizer implementation if importable, else inline."""
    if _is_well_formed_clause is not None:
        try:
            return bool(_is_well_formed_clause(value))
        except Exception:
            return _inline_well_formed_clause(value)
    return _inline_well_formed_clause(value)


_CONTRACT_PREFIXES = ("contract:", "preserve:")


def _is_contract_line(stripped: str) -> bool:
    low = stripped.lower()
    if low.startswith(_CONTRACT_PREFIXES):
        return True
    if "preserve " in low:
        return True
    if "guard_clause:" in low:
        return True
    return False


def _extract_guard_values(stripped: str) -> list[str]:
    """Return the guard value(s) carried by a contract/preserve line.

    Strips the leading 'Contract:' / 'Preserve:' / 'guard_clause:' label, then
    splits on the contract-clause separator '|' so each clause is checked on its
    own (a balanced clause must not be flagged because a sibling clause exists).
    """
    val = stripped
    low = val.lower()
    for pref in ("contract:", "preserve:"):
        if low.startswith(pref):
            val = val[len(pref):]
            break
    else:
        idx = low.find("guard_clause:")
        if idx != -1:
            val = val[idx + len("guard_clause:"):]
    parts = [p.strip() for p in val.split("|")]
    return [p for p in parts if p]


def _scan_contract_lines(instr: str) -> tuple[bool, list[str]]:
    """Scan agent-facing instruction lines for malformed contract guards.

    Returns (any_malformed, list_of_malformed_descriptions). Computed always;
    only gated to a FAIL by --require-balanced-contracts.
    """
    malformed: list[str] = []
    for line in instr.splitlines():
        stripped = line.strip()
        if not stripped or not _is_contract_line(stripped):
            continue
        for guard in _extract_guard_values(stripped):
            if not _clause_is_well_formed(guard):
                malformed.append(guard)
    return (bool(malformed), malformed)


# --------------------------------------------------------------------------- #
# C1 in OBSERVATION content: balanced post-edit guard well-formedness          #
#                                                                              #
# The instruction-only scan above missed the haystack C1: the truncated guard  #
# lived in a POST-EDIT L3/L3b OBSERVATION line (`SEMANTIC WARNING:` / `[RAISES]`#
# / `PRESERVE:`), not in the first-turn instruction. These markers carry the   #
# same kind of guard value (a `raise`/condition/return fragment) and so suffer #
# the same mid-token clip. Each marker is a label prefix; the guard value is   #
# whatever follows the marker on that line, validated by the SAME              #
# `_clause_is_well_formed` contract — language-agnostic (quotes/brackets/      #
# dangling-operator only), so no marker/repo/literal is hardcoded beyond the   #
# structural label set.                                                        #
# --------------------------------------------------------------------------- #

# Guard-bearing markers that appear in agent-visible OBSERVATION content. Each
# is a structural label that introduces a code/expression fragment; matched
# case-insensitively. Bracket markers are matched by their closing bracket so
# the value is taken from after `]`.
_OBS_BRACKET_MARKERS = ("[raises]", "[catches]")
_OBS_PREFIX_MARKERS = ("preserve:", "semantic warning:", "guard_clause:", "preserve ")

# A producer sometimes appends a structural closing tag (</gt-context>, </gt-evidence>)
# directly after a guard value. The tag is NOT part of the guard, and its '>' must
# not read as a dangling operator. Strip trailing tags before validation. This cannot
# repair a genuinely malformed guard (an unterminated string / unbalanced bracket
# survives the strip), so real defects are still caught.
_TRAILING_TAG_RE = re.compile(r"(?:\s*</?[\w:.-]+>)+\s*$")


def _strip_trailing_tags(s: str) -> str:
    return _TRAILING_TAG_RE.sub("", s).rstrip()


def _extract_observation_guards(line: str) -> list[str]:
    """Return guard value(s) carried by a guard-bearing OBSERVATION line.

    Finds the LAST occurrence of any known guard marker on the line and returns
    the trailing fragment as the guard value. Returns [] when the line carries
    no marker. A sub-label like 'New guard:' that the producer prepends to the
    value is left intact — the well-formedness contract only cares about
    quote/bracket balance, so descriptive prose before a `raise(...)` does not
    create a false positive, while a clipped literal inside it still trips.
    """
    low = line.lower()
    best_idx = -1
    best_end = -1
    for mk in _OBS_BRACKET_MARKERS:
        idx = low.rfind(mk)
        if idx != -1 and idx > best_idx:
            best_idx, best_end = idx, idx + len(mk)
    for mk in _OBS_PREFIX_MARKERS:
        idx = low.rfind(mk)
        if idx != -1 and idx > best_idx:
            best_idx, best_end = idx, idx + len(mk)
    if best_idx == -1:
        return []
    value = _strip_trailing_tags(line[best_end:].strip())
    return [value] if value else []


# A guard value can span physical lines (`(a or\n  b)` / `... ->\n  result`). The
# per-line scan must JOIN continuation lines before flagging — a balanced multi-line
# guard is not malformed. Stop joining at a blank line or a NEW marker.
_NEW_MARKER_RE = re.compile(r"^\[[A-Z]")


def _starts_new_marker(stripped: str) -> bool:
    low = stripped.lower()
    return (bool(_NEW_MARKER_RE.match(stripped))
            or any(low.startswith(m) for m in _OBS_PREFIX_MARKERS)
            or low.startswith("contract:"))


def _scan_observation_guards(path: Path) -> tuple[bool, list[str]]:
    """Scan agent OBSERVATION `content` lines for malformed guard fragments.

    Walks every history entry (the same record shapes `_scan_layer_markers`
    handles) and reads ONLY `_observation_content` — never the instruction,
    never telemetry fields (gt_layer_event, args, metadata). Computed always;
    only gated to a FAIL by --require-balanced-contracts. Returns
    (any_malformed, list_of_malformed_guard_values).
    """
    malformed: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            hist = rec.get("history")
            entries: list = []
            if isinstance(hist, list):
                entries = [e for e in hist if isinstance(e, dict)]
            elif rec.get("history") is None and "content" in rec and "instruction" not in rec:
                # tolerate a flat per-entry JSONL shape (one history entry per line)
                entries = [rec]
            for e in entries:
                content = _observation_content(e)
                if not content:
                    continue
                obs_lines = content.splitlines()
                for idx, line in enumerate(obs_lines):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    for guard in _extract_observation_guards(stripped):
                        if _clause_is_well_formed(guard):
                            continue
                        # Multi-line guard: a balanced guard split across physical
                        # lines leaves a dangling `or`/open `(` on this line. Join
                        # continuation lines and re-check; flag only if it stays
                        # malformed (a genuine unterminated/unbalanced value).
                        joined = guard
                        resolved_ok = False
                        for j in range(idx + 1, min(idx + 4, len(obs_lines))):
                            nxt = obs_lines[j].strip()
                            if not nxt or _starts_new_marker(nxt):
                                break
                            joined = (joined + " " + _strip_trailing_tags(nxt)).strip()
                            if _clause_is_well_formed(joined):
                                resolved_ok = True
                                break
                        if not resolved_ok:
                            malformed.append(guard)
    return (bool(malformed), malformed)


# --------------------------------------------------------------------------- #
# C1 SAFE-RENDER independent semantic checks (--require-safe-render).           #
#                                                                              #
# These do NOT merely call the sanitizer and compare; they INDEPENDENTLY check #
# the delivered agent-facing content, so an incomplete sanitizer is still      #
# caught. All rules here are GENERAL (structural / language-agnostic). The     #
# harness file-read banners (`Here's the result of running` / `cat -n` /       #
# `# SPDX`) are NOT used as gate rules — markerless glue is PREVENTED at the    #
# boundary (sanitizer.join_without_glue) and appears only as a regression      #
# FIXTURE. The truncated-marker rule is the general glue signature.            #
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - both branches exercised across run modes
    from groundtruth.runtime.sanitizer import valid_exception_spec as _ext_valid_exception_spec  # type: ignore
    from groundtruth.runtime.sanitizer import sanitize_evidence_block as _ext_sanitize_block  # type: ignore
except Exception:
    _ext_valid_exception_spec = None  # type: ignore[assignment]
    _ext_sanitize_block = None  # type: ignore[assignment]

_INLINE_EXC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_INLINE_STMT_KEYWORDS = frozenset({
    "raise", "return", "throw", "throws", "yield", "if", "else", "elif", "for",
    "while", "try", "except", "catch", "finally", "with", "def", "fn", "func",
    "class", "struct", "import", "from", "pass", "break", "continue", "and",
    "or", "not", "in", "is", "lambda", "async", "await", "del", "global",
    "nonlocal", "assert", "match", "case", "new", "panic", "defer", "go",
})


def _inline_valid_exception_spec(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    parts = [p.strip() for p in s.split(",")]
    if any(not p for p in parts):
        return False
    for p in parts:
        if not _INLINE_EXC_NAME_RE.match(p):
            return False
        if any(seg in _INLINE_STMT_KEYWORDS for seg in p.split(".")):
            return False
    return True


def _valid_exc_spec(s: str) -> bool:
    """Dispatch to sanitizer.valid_exception_spec if importable, else inline."""
    if _ext_valid_exception_spec is not None:
        try:
            return bool(_ext_valid_exception_spec(s))
        except Exception:
            return _inline_valid_exception_spec(s)
    return _inline_valid_exception_spec(s)


# Known single-word GT bracket markers. The truncated-marker rule is ANCHORED on
# this set so ordinary bracketed text / type hints (`[Optional]`, `List[Document]`)
# can never be mistaken for a cut marker. General; contains no harness string and
# no repo/literal — only GT's own marker vocabulary.
_GT_MARKER_NAMES = (
    "CATCHES", "RAISES", "RETURNS", "SIGNATURE", "CONTRACT", "CALLER", "CALLS",
    "BOUNDARY", "TWIN", "PEER", "MISMATCH", "READS", "PROPAGATE", "SCOPE",
    "RECALL", "CONCURRENCY", "RESOURCE", "SECURITY", "ORDER", "SERDE", "FIELD",
)
_SEG_SPLIT_RE = re.compile(r"\s\|\s")


def _scan_exception_specs(instr: str) -> list[str]:
    """Rule 1: every `Contract: ... raises <spec> ...` token is a valid exception
    identifier/dotted class name. Independent of the sanitizer."""
    bad: list[str] = []
    for line in instr.splitlines():
        s = line.strip()
        if not s.lower().startswith("contract:"):
            continue
        body = s[len("contract:"):].strip()
        for seg in _SEG_SPLIT_RE.split(body):
            seg = seg.strip()
            if seg.lower().startswith("raises "):
                spec = seg[len("raises"):].strip()
                if spec and not _valid_exc_spec(spec):
                    bad.append(spec)
    return bad


def _scan_empty_fields(instr: str) -> list[str]:
    """Rule 2: a contract field label with an empty/bare value (bare `raises`/
    `returns`, empty `guard_clause:`/`return_shape:`/`Preserve:`)."""
    bad: list[str] = []
    for line in instr.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("contract:"):
            for seg in _SEG_SPLIT_RE.split(s[len("contract:"):].strip()):
                if seg.strip().lower() in ("raises", "returns"):
                    bad.append(seg.strip())
        elif low.startswith("preserve:"):
            rest = s[len("preserve:"):].strip()
            m = re.match(r"^(\w+):\s*(.*)$", rest)
            value = m.group(2) if m else rest
            if not value.strip():
                bad.append(s)
    return bad


def _scan_truncated_markers(text: str) -> list[str]:
    """Rule 5: a KNOWN GT bracket marker cut before its closing `]` and fused to
    text (`[CATCHEHere's`). Anchored on the GT marker name set, so ordinary
    bracketed text / type hints (`[Optional]`, `List[Document]`) never match.
    General; no harness string, no repo literal."""
    found: set[str] = set()
    full_markers = tuple("[" + marker + "]" for marker in _GT_MARKER_NAMES)
    for name in _GT_MARKER_NAMES:
        full = "[" + name + "]"
        for L in range(len(name), 3, -1):          # full name down to 4 chars
            cut = "[" + name[:L]
            contchar = name[L] if L < len(name) else ""
            i = text.find(cut)
            while i >= 0:
                nxt = text[i + len(cut): i + len(cut) + 1]
                if text.startswith(full_markers, i):
                    pass                            # intact marker, fine
                elif nxt and nxt.isalnum() and nxt != contchar:
                    # cut + GLUED to a word — the verified signature `[CATCHEHere`.
                    # A space/modifier after the name (`[CONTRACT ~]`, `[RAISES ?]`)
                    # is an INTACT marker variant, not a cut; require an alnum fuse.
                    found.add(cut + "…")
                i = text.find(cut, i + 1)
    return sorted(found)


def _brief_region(instr: str) -> str:
    """The GT brief block (from <gt-task-brief> up to the original instruction
    body) — the scope for the idempotence backstop."""
    i = instr.find("<gt-task-brief>")
    if i < 0:
        return ""
    rest = instr[i:]
    for marker in ("<uploaded_files>", "I've uploaded a", "Consider the following issue"):
        j = rest.find(marker)
        if j > 0:
            rest = rest[:j]
            break
    return rest.rstrip()


def _scan_observation_truncated_markers(path: Path) -> list[str]:
    """Rule 5 over agent OBSERVATION content (the `[CATCHEHere's` glue lived in a
    post-edit observation, not the instruction)."""
    found: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            hist = rec.get("history")
            if isinstance(hist, list):
                entries = [e for e in hist if isinstance(e, dict)]
            elif rec.get("history") is None and "content" in rec and "instruction" not in rec:
                entries = [rec]
            else:
                entries = []
            for e in entries:
                content = _observation_content(e)
                if content:
                    found.extend(_scan_truncated_markers(content))
    return found


# --------------------------------------------------------------------------- #
# Layer-marker presence in agent OBSERVATION content (trigger-gated)           #
# --------------------------------------------------------------------------- #

# Substrings in an action's type/name that mark a file mutation (edit trigger).
_EDIT_ACTION_HINTS = ("edit", "write", "str_replace", "create", "insert", "patch", "apply")
# Substrings marking a review / submission / finish transition.
_REVIEW_ACTION_HINTS = ("review", "submit", "finish", "complete", "done", "pr_create")


def _action_kind(entry: dict) -> str:
    """Best-effort lowercase action label for a history entry.

    Reads only fields that name an action/tool — never observation content, so a
    GT marker inside `content` can't be mistaken for an action name.
    """
    for key in ("action", "action_type", "tool", "tool_name", "name", "function", "type"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v.lower()
        if isinstance(v, dict):
            inner = v.get("name") or v.get("type")
            if isinstance(inner, str) and inner:
                return inner.lower()
    return ""


def _observation_content(entry: dict) -> str:
    """Agent-visible observation text for a history entry.

    Only `content`/`message` count. Telemetry fields (gt_layer_event, args,
    metadata, gt_brief*) are deliberately excluded — they are not what the agent
    observed.
    """
    c = entry.get("content")
    if isinstance(c, str) and c:
        return c
    m = entry.get("message")
    if isinstance(m, str) and m:
        return m
    return ""


def _iter_agent_text(path: Path):
    """Yield only text the agent could see: instruction plus history content."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            instr = rec.get("instruction")
            if isinstance(instr, str) and instr:
                yield ("instruction", instr)
            hist = rec.get("history")
            entries: list = []
            if isinstance(hist, list):
                entries = [e for e in hist if isinstance(e, dict)]
            elif rec.get("history") is None and "content" in rec and "instruction" not in rec:
                entries = [rec]
            for e in entries:
                content = _observation_content(e)
                if content:
                    yield ("observation", content)


def _runtime_delivery_issues(path: Path) -> dict:
    """Runtime proof checks over agent-visible text, not logs/telemetry."""
    observation_leaks: set[str] = set()
    dead_markers: set[str] = set()
    duplicate_gt: list[str] = []
    seen_blocks: dict[str, int] = {}
    for surface, text in _iter_agent_text(path):
        for marker in HIDDEN_DIAG_MARKERS:
            if marker in text and surface == "observation":
                observation_leaks.add(marker)
        for marker in DEAD_PATH_MARKERS:
            if marker in text:
                dead_markers.add(marker)
        if surface != "observation":
            continue
        if any(marker in text for marker in GT_OBSERVATION_MARKERS):
            normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
            if normalized:
                seen_blocks[normalized] = seen_blocks.get(normalized, 0) + 1
    for block, count in seen_blocks.items():
        if count > 1:
            duplicate_gt.append(block[:160])
    return {
        "observation_leaked_markers": sorted(observation_leaks),
        "dead_path_markers": sorted(dead_markers),
        "duplicate_gt_observations": duplicate_gt,
    }


def _scan_layer_markers(path: Path) -> dict:
    """Walk every history entry; record edit/review triggers and observed markers.

    Returns a dict with: edit_seen, review_seen, edit_review_transition,
    l3_evidence_seen, l3b_contract_seen, l6_verify_seen.
    """
    out = {
        "edit_seen": False,
        "review_seen": False,
        "edit_review_transition": False,
        "l3_evidence_seen": False,
        "l3b_contract_seen": False,
        "l6_verify_seen": False,
    }
    saw_edit = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            hist = rec.get("history")
            entries: list = []
            if isinstance(hist, list):
                entries = [e for e in hist if isinstance(e, dict)]
            elif rec.get("history") is None and "content" in rec and "instruction" not in rec:
                # tolerate a flat per-entry JSONL shape (one history entry per line)
                entries = [rec]
            for e in entries:
                kind = _action_kind(e)
                if kind:
                    if any(h in kind for h in _EDIT_ACTION_HINTS):
                        saw_edit = True
                        out["edit_seen"] = True
                    if any(h in kind for h in _REVIEW_ACTION_HINTS):
                        out["review_seen"] = True
                        if saw_edit:
                            out["edit_review_transition"] = True
                content = _observation_content(e)
                if not content:
                    continue
                # L3 post-edit evidence reaches the agent in two real forms: the
                # legacy block opens with an ATTRIBUTED tag
                # `<gt-evidence trigger="post_edit:...">` (NOT a bare `<gt-evidence>`),
                # and the router_v2=live path leads with `[GT] Post-edit:`. Match
                # either — a bare-tag-only check is a false negative on live runs.
                if "<gt-evidence" in content or "[GT] Post-edit:" in content:
                    out["l3_evidence_seen"] = True
                if "[CONTRACT]" in content:
                    out["l3b_contract_seen"] = True
                if "[GT_VERIFY] Tests covering" in content:
                    out["l6_verify_seen"] = True
    return out


def check_brief_delivery(
    path: str,
    *,
    require_graph_map: bool = False,
    require_contract_line: bool = False,
    allow_empty_graph_map: bool = False,
    require_balanced_contracts: bool = False,
    require_layer_markers: bool = False,
    require_safe_render: bool = False,
    require_runtime_delivery: bool = False,
) -> dict:
    p = Path(path)
    result: dict = {
        "check": "check_brief_delivery",
        "path": str(p),
        "passed": False,
        "instruction_len": 0,
        "task_brief_open": 0,
        "task_brief_close": 0,
        "graph_map_present": False,
        "graph_map_body_len": 0,
        "leak_found": False,
        "leaked_markers": [],
        # C1 balanced-contract guard (computed always, gated by flag)
        "malformed_contract_found": False,
        "malformed_contracts": [],
        # C1 in agent OBSERVATION content (post-edit L3/L3b guards; computed
        # always, gated by the same --require-balanced-contracts flag).
        "malformed_observation_guards": [],
        # C1 SAFE-RENDER independent semantic checks (computed always; gated by
        # --require-safe-render). General/structural — no harness strings.
        "bad_exception_specs": [],
        "empty_contract_fields": [],
        "truncated_markers": [],
        "brief_idempotent": None,
        "balanced_clause_impl": "sanitizer" if _USING_SANITIZER_CLAUSE else "inline",
        # layer-marker presence (computed always, gated by flag)
        "edit_seen": False,
        "review_seen": False,
        "edit_review_transition": False,
        "l3_evidence_seen": False,
        "l3b_contract_seen": False,
        "l6_verify_seen": False,
        "observation_leaked_markers": [],
        "dead_path_markers": [],
        "duplicate_gt_observations": [],
        "reasons": [],
    }
    if not p.exists():
        result["reasons"].append(f"file not found: {p}")
        return result

    instr = extract_first_turn_instruction(p)
    if not instr:
        result["reasons"].append("no agent-facing instruction containing <gt-task-brief> found")
        return result

    result["instruction_len"] = len(instr)
    result["task_brief_open"] = instr.count("<gt-task-brief>")
    result["task_brief_close"] = instr.count("</gt-task-brief>")
    present, body_len = _graph_map_body(instr)
    result["graph_map_present"] = present
    result["graph_map_body_len"] = body_len
    leaked = sorted({m for m in HIDDEN_DIAG_MARKERS if m in instr})
    result["leak_found"] = bool(leaked)
    result["leaked_markers"] = leaked

    # C1: balanced-contract scan (always computed) over agent-facing instruction.
    instr_malformed_found, malformed = _scan_contract_lines(instr)
    result["malformed_contracts"] = malformed

    # C1 (observation side): scan post-edit L3/L3b OBSERVATION guards too — the
    # haystack regression that the instruction-only scan missed. Always computed.
    obs_malformed_found, obs_malformed = _scan_observation_guards(p)
    result["malformed_observation_guards"] = obs_malformed

    # malformed_contract_found summarizes EITHER side being malformed so the
    # legacy field stays a single C1 verdict; the per-side lists disambiguate.
    result["malformed_contract_found"] = instr_malformed_found or obs_malformed_found

    # C1 SAFE-RENDER independent checks (rules 1,2,5,6 — always computed, gated
    # by --require-safe-render). Independent of the sanitizer's own output.
    bad_exc = _scan_exception_specs(instr)
    empty_fields = _scan_empty_fields(instr)
    trunc = _scan_truncated_markers(instr) + _scan_observation_truncated_markers(p)
    result["bad_exception_specs"] = bad_exc
    result["empty_contract_fields"] = empty_fields
    result["truncated_markers"] = trunc
    region = _brief_region(instr)
    if _ext_sanitize_block is not None and region:
        try:
            result["brief_idempotent"] = (_ext_sanitize_block(region) == region)
        except Exception:
            result["brief_idempotent"] = None

    # Layer markers (always computed) over agent observation content.
    layer = _scan_layer_markers(p)
    result["edit_seen"] = layer["edit_seen"]
    result["review_seen"] = layer["review_seen"]
    result["edit_review_transition"] = layer["edit_review_transition"]
    result["l3_evidence_seen"] = layer["l3_evidence_seen"]
    result["l3b_contract_seen"] = layer["l3b_contract_seen"]
    result["l6_verify_seen"] = layer["l6_verify_seen"]
    runtime = _runtime_delivery_issues(p)
    result["observation_leaked_markers"] = runtime["observation_leaked_markers"]
    result["dead_path_markers"] = runtime["dead_path_markers"]
    result["duplicate_gt_observations"] = runtime["duplicate_gt_observations"]

    reasons = result["reasons"]
    if result["task_brief_open"] != 1:
        reasons.append(f"expected exactly 1 <gt-task-brief> open tag, found {result['task_brief_open']}")
    if result["task_brief_close"] != 1:
        reasons.append(f"expected exactly 1 </gt-task-brief> close tag, found {result['task_brief_close']}")
    if present and body_len == 0 and not allow_empty_graph_map:
        reasons.append("<gt-graph-map> present but body is EMPTY (delivery shred)")
    if require_graph_map and (not present or body_len == 0):
        reasons.append("--require-graph-map: <gt-graph-map> missing or empty")
    if leaked:
        reasons.append(f"hidden diagnostic leakage in agent instruction: {leaked}")
    if require_contract_line and "Contract:" not in instr:
        reasons.append("--require-contract-line: no 'Contract:' line in the brief")
    if require_balanced_contracts and instr_malformed_found:
        reasons.append(
            "--require-balanced-contracts: malformed contract guard(s) in "
            f"instruction (unbalanced/unterminated/dangling): {malformed}"
        )
    if require_balanced_contracts and obs_malformed_found:
        reasons.append(
            "--require-balanced-contracts: malformed guard(s) in agent "
            "observation content "
            f"(unbalanced/unterminated/dangling): {obs_malformed}"
        )
    if require_safe_render:
        # Rule 1: exception tokens
        if bad_exc:
            reasons.append(f"--require-safe-render: invalid exception spec(s) in Contract raises: {bad_exc}")
        # Rule 2: empty/bare required fields
        if empty_fields:
            reasons.append(f"--require-safe-render: empty/bare contract field(s): {empty_fields}")
        # Rule 5: truncated GT marker prefix (glue signature) — general
        if trunc:
            reasons.append(f"--require-safe-render: truncated GT marker prefix(es) (raw-cut glue): {trunc[:8]}")
        # Rules 3,4: dangling operator / unterminated quote (reuse existing scan)
        if instr_malformed_found or obs_malformed_found:
            reasons.append(
                "--require-safe-render: malformed guard(s) (dangling op / unterminated): "
                f"instr={malformed} obs={obs_malformed}"
            )
        # Rule 6 (LAST, backstop): sanitizer idempotence on the brief region
        if result["brief_idempotent"] is False:
            reasons.append(
                "--require-safe-render: brief is not Safe-Renderer-idempotent "
                "(the Safe Renderer would change the delivered brief)"
            )
    if require_layer_markers:
        if result["edit_seen"] and not result["l3_evidence_seen"]:
            reasons.append(
                "--require-layer-markers: edit occurred but no L3 evidence "
                "(`<gt-evidence` block or `[GT] Post-edit:`) in any agent observation"
            )
        if result["edit_seen"] and not result["l3b_contract_seen"]:
            reasons.append(
                "--require-layer-markers: edit occurred but no L3b [CONTRACT] "
                "line in any agent observation"
            )
        if result["edit_review_transition"] and not result["l6_verify_seen"]:
            reasons.append(
                "--require-layer-markers: edit->review transition occurred but no "
                "L6 '[GT_VERIFY] Tests covering' line in any agent observation"
            )
    if require_runtime_delivery:
        if result["observation_leaked_markers"]:
            reasons.append(
                "--require-runtime-delivery: hidden diagnostic leakage in agent "
                f"observation: {result['observation_leaked_markers']}"
            )
        if result["dead_path_markers"]:
            reasons.append(
                "--require-runtime-delivery: retired/dead path marker reached agent: "
                f"{result['dead_path_markers']}"
            )
        if result["duplicate_gt_observations"]:
            reasons.append(
                "--require-runtime-delivery: duplicate GT observation block(s) reached agent"
            )

    result["passed"] = not reasons
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Prove L1 GT brief delivery in a real output.jsonl")
    ap.add_argument("path", help="path to output.jsonl")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--require-graph-map", action="store_true")
    ap.add_argument("--require-contract-line", action="store_true")
    ap.add_argument("--allow-empty-graph-map", action="store_true", default=False)
    ap.add_argument("--require-balanced-contracts", action="store_true", default=False,
                    help="FAIL if any Contract:/Preserve: instruction guard OR any "
                         "PRESERVE:/[RAISES]/[CATCHES]/SEMANTIC WARNING: observation guard "
                         "is malformed (C1 regression guard)")
    ap.add_argument("--require-layer-markers", action="store_true", default=False,
                    help="FAIL if a triggered L3/L3b/L6 marker is absent from agent observations")
    ap.add_argument("--require-safe-render", action="store_true", default=False,
                    help="FAIL on invalid exception spec / empty-bare contract field / truncated "
                         "GT marker prefix / dangling-unterminated guard / non-idempotent brief "
                         "(C1 independent semantic checks; general, no harness strings)")
    ap.add_argument("--require-runtime-delivery", action="store_true", default=False,
                    help="FAIL on hidden diagnostic leakage in observations, duplicate GT "
                         "observation delivery, or retired-path markers in agent-visible text")
    args = ap.parse_args()

    r = check_brief_delivery(
        args.path,
        require_graph_map=args.require_graph_map,
        require_contract_line=args.require_contract_line,
        allow_empty_graph_map=args.allow_empty_graph_map,
        require_balanced_contracts=args.require_balanced_contracts,
        require_layer_markers=args.require_layer_markers,
        require_safe_render=args.require_safe_render,
        require_runtime_delivery=args.require_runtime_delivery,
    )
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"[{verdict}] {r['path']}")
        print(f"  instruction_len={r['instruction_len']} "
              f"task_brief_open={r['task_brief_open']} task_brief_close={r['task_brief_close']}")
        print(f"  graph_map_present={r['graph_map_present']} graph_map_body_len={r['graph_map_body_len']}")
        print(f"  leak_found={r['leak_found']} leaked_markers={r['leaked_markers']}")
        print(f"  malformed_contract_found={r['malformed_contract_found']} "
              f"({r['balanced_clause_impl']}) malformed={r['malformed_contracts']}")
        print(f"  malformed_observation_guards={r['malformed_observation_guards']}")
        print(f"  bad_exception_specs={r['bad_exception_specs']} empty_contract_fields={r['empty_contract_fields']}")
        print(f"  truncated_markers={r['truncated_markers'][:8]} brief_idempotent={r['brief_idempotent']}")
        print(f"  edit_seen={r['edit_seen']} edit_review_transition={r['edit_review_transition']} "
              f"l3_evidence_seen={r['l3_evidence_seen']} l3b_contract_seen={r['l3b_contract_seen']} "
              f"l6_verify_seen={r['l6_verify_seen']}")
        print(f"  observation_leaked_markers={r['observation_leaked_markers']} "
              f"dead_path_markers={r['dead_path_markers']} "
              f"duplicate_gt_observations={len(r['duplicate_gt_observations'])}")
        for reason in r["reasons"]:
            print(f"  - {reason}")
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
