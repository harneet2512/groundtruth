"""Trajectory-derived per-run AND per-layer metrics for GroundTruth.

GroundTruth (GT) is an LLM-free layer that injects context into an AI coding
agent. This module MEASURES one run from its trajectory (``output.jsonl``),
NOT by grepping logs. It replaces the canary's grep-based summary.

Hard rules enforced here:
  * Deterministic — no LLM, no randomness, stable sorted ordering.
  * ZERO grep / no regex over raw-log bytes — the history is parsed
    structurally as JSON. (One small regex is used only to split a unified
    diff's ``+++ b/`` headers — that is a structured patch field, not a log
    byte stream, and is reused from ``evidence_scorer.parse_gold_patch``.)
  * Count AGENT ACTIONS (run+edit+read), not raw events; ``error_count`` is
    reported separately.
  * "fired" (GT tried) comes from the gt_log layer field IF provided;
    "delivered" (agent saw) comes ONLY from the trajectory observations.
    They are never conflated (fired != delivered).
  * Generalized — ``gold_files`` is an INPUT; there is no repo/task hardcoding.

output.jsonl schema (verified against real beets artifacts):
  ONE line = ONE instance JSON with keys: instance_id, instruction, history
  (list), test_result(.git_patch), instance, metadata, metrics, error.
  Each history event: {id, timestamp, source, message, action OR observation,
  args, content}. ``source`` in {agent, user, environment}. Agent action in
  {system, message, recall, task_tracking, run, edit, read, error}. Observation
  events carry an ``observation`` field plus the agent-visible OUTPUT in
  ``content`` (and a short summary in ``message``). GT-injected content
  (``[GT]..``, ``<gt-...>``) is prepended/appended there — that is the
  AGENT-VISIBLE channel and the source of truth for "delivered".

This module only reads; it writes ``run_metrics.json`` on request.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Iterable, Optional

# Reuse — do NOT duplicate — the gold-patch parser and injection parser.
from groundtruth.metrics.evidence_scorer import (
    InjectionRecord,
    parse_gold_patch,
    parse_injections,
)

# ---------------------------------------------------------------------------
# Per-layer marker map. A layer is identified by ANY of its marker strings
# appearing in an agent-visible observation's text.
# ---------------------------------------------------------------------------
LAYER_MARKERS: dict[str, list[str]] = {
    "L1_brief": ["<gt-task-brief>"],
    "L3_postedit": [
        'trigger="post_edit', "[GT] Post-edit:",
        # post-edit content markers (per task marker map): a post-edit block
        # carries these even when the wrapper omits the explicit trigger tag.
        "[BEHAVIORAL CONTRACT]", "Calls into:",
    ],
    "postview": ['trigger="post_view'],
    "L3b_similar": ["[SIMILAR]", "[PEER]", "[TWIN]"],
    "L5_advisory": ["[GT L5"],
    "L6_verify": ["[GT_VERIFY]"],
    "grep_intercept": ["[GT] Called by:", "[GT] "],
    "curation": ["[GT_CURATION]"],
}

# A layer's content is considered post_view-style (a [GT] block appended onto a
# read/grep result) when it carries these substrings.
_GREP_INTERCEPT_HINT = "[GT] Called by:"

# New-marker sub-detectors (must NOT false-positive on OLD runs lacking them).
_EDIT_TARGET_CONTRACTS = "EDIT-TARGET CONTRACTS"
_TWIN_MARKERS = ("[TWIN]",)
_SIMILAR_MARKERS = ("[SIMILAR]", "[PEER]", "[TWIN]")

# Regression-guard marker openers (a truncated opener glued onto a word with no
# closing ']' is corruption).
_MARKER_OPENERS = (
    "[SIGNATURE", "[BEHAVIORAL CONTRACT", "[CONTRACT", "[CATCHES", "[CATCHE",
    "[SIMILAR", "[PEER", "[TWIN", "[VERIFIED", "[WARNING", "[INFO", "[TEST",
    "[MISMATCH", "[GT_VERIFY", "[GT L5", "[OVERRIDE", "[PATTERN", "[COMPLETENESS",
)

# Empty-contract sentinel labels: a line "<label>:" whose value is blank.
_CONTRACT_LABELS = (
    "PRESERVE:", "guard_clause:", "[BEHAVIORAL CONTRACT]", "MUTATES:",
    "RETURNS:", "RAISES:",
)


# ---------------------------------------------------------------------------
# Low-level history access
# ---------------------------------------------------------------------------
def load_instance(output_jsonl_path: str) -> dict[str, Any]:
    """Load the (single-instance) output.jsonl line as a dict.

    If the file holds multiple instance lines, the first non-empty line wins
    (deterministic: file order). Returns {} if the file is missing/empty.
    """
    if not output_jsonl_path or not os.path.exists(output_jsonl_path):
        return {}
    with open(output_jsonl_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
    return {}


def _norm_path(p: str) -> str:
    """Normalize a path: backslashes -> slashes, strip leading slashes."""
    return (p or "").replace("\\", "/").lstrip("/")


def _path_suffix_match(a: str, b: str) -> bool:
    """True if one normalized path is a trailing-segment suffix of the other.

    Mirrors evidence_scorer's matcher so gold-file matching is consistent.
    """
    a, b = _norm_path(a), _norm_path(b)
    if not a or not b:
        return False
    pa, pb = a.split("/"), b.split("/")
    shorter = pa if len(pa) <= len(pb) else pb
    longer = pb if len(pa) <= len(pb) else pa
    return longer[-len(shorter):] == shorter if shorter else False


def _gold_basenames(gold_files: Iterable[str]) -> set[str]:
    return {_norm_path(g).split("/")[-1] for g in gold_files if g}


def _is_counted_action(ev: dict) -> bool:
    """A counted agent action = source 'agent' and action in run/edit/read."""
    return ev.get("source") == "agent" and ev.get("action") in ("run", "edit", "read")


def _action_target_text(ev: dict) -> str:
    """The path/command text an agent action operates on (for gold matching)."""
    args = ev.get("args") or {}
    parts = []
    for k in ("path", "command"):
        v = args.get(k)
        if isinstance(v, str) and v:
            parts.append(v)
    return _norm_path(" ".join(parts)) if parts else ""


# Source-file extensions used to recognize a file token inside a command line.
_SOURCE_EXTS = (
    ".py", ".rst", ".md", ".txt", ".go", ".js", ".ts", ".java", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cc",
)


def _file_tokens(text: str) -> set[str]:
    """File-path-looking tokens in a run/read command or path string.

    A token qualifies if, after normalization and stripping shell punctuation,
    it ends in a known source extension. Both ``beets/importer.py`` and a bare
    ``importer.py`` qualify; ``set_fields`` (no extension) does not. Handles the
    ``path:line`` form (``beets/db.py:722``) by stripping a trailing ``:NNN``.
    """
    tokens: set[str] = set()
    norm = _norm_path(text)
    # Split on whitespace and common shell separators so pipes/redirects don't
    # glue a file onto the next token (e.g. "...importer.py|head").
    for raw in norm.replace("|", " ").replace(";", " ").replace("(", " ").replace(
        ")", " "
    ).split():
        t = raw.strip().strip('"').strip("'").strip("`")
        # Strip a trailing ":NNN" line-number suffix ("beets/db.py:722").
        if ":" in t:
            head, _, tail = t.rpartition(":")
            if head and tail.isdigit():
                t = head
        if t.endswith(_SOURCE_EXTS):
            tokens.add(t)
    return tokens


def _action_reaches_gold(ev: dict, gold: list[str]) -> bool:
    """True if a run/read action operates on a gold file.

    Uses the SAME rigorous path-suffix matching as ``edit_to_gold_action``
    (``_path_suffix_match``) on file-path tokens extracted from the action's
    command/path — NOT a loose substring test. This prevents ``cat
    notimporter.py`` from registering as reaching gold ``importer.py`` while
    still matching ``grep set_fields beets/importer.py`` and
    ``sed -n ... beets/importer.py``.
    """
    if not gold:
        return False
    # Prefer the explicit path arg; otherwise scan the whole command text.
    args = ev.get("args") or {}
    explicit = args.get("path")
    candidates: set[str] = set()
    if isinstance(explicit, str) and explicit:
        candidates |= _file_tokens(explicit)
    cmd = args.get("command")
    if isinstance(cmd, str) and cmd:
        candidates |= _file_tokens(cmd)
    for tok in candidates:
        if any(_path_suffix_match(tok, g) for g in gold):
            return True
    return False


def _visible_texts(ev: dict) -> list[str]:
    """Agent-visible text fields of an event, de-duplicated.

    The agent sees observation ``content`` (full tool result, where GT content
    is injected) and the GT pre-task brief ``message`` (user message). We scan
    both ``content`` and ``message`` but drop exact duplicates so a marker that
    appears in both fields of one event is counted once per event.
    """
    out: list[str] = []
    seen: set[str] = set()
    for k in ("content", "message"):
        v = ev.get(k)
        if isinstance(v, str) and v and v not in seen:
            out.append(v)
            seen.add(v)
    return out


def _is_agent_visible(ev: dict) -> bool:
    """Events whose text the agent actually observes.

    Two channels:
      1. Observation events (any event carrying an ``observation`` key) — these
         are tool results; GT post_view/post_edit/verify content is injected
         here. In OpenHands these carry source 'agent' or 'environment'.
      2. The GT pre-task brief: a user 'message' (and its 'recall' echo).
    """
    if ev.get("observation"):
        return True
    if ev.get("source") == "user" and ev.get("action") in ("message", "recall"):
        return True
    return False


# ---------------------------------------------------------------------------
# RUN-LEVEL: localization (from the brief's ranked file list)
# ---------------------------------------------------------------------------
def _extract_brief_ranked_files(instance: dict) -> list[str]:
    """Parse the GT pre-task brief into an ordered list of ranked file paths.

    The brief is a user 'message' containing '<gt-task-brief>' and lines like
    '1. beets/ui/__init__.py (def ...)'. We read the leading 'N. <path>' token
    of each ranked line, in rank order. Lines that are sub-bullets ('Calls:',
    'Tests:', 'Contract:') are NOT ranked files.
    """
    history = instance.get("history") or []
    brief_text = ""
    for ev in history:
        if ev.get("source") == "user" and ev.get("action") == "message":
            msg = ev.get("message") or ""
            if "<gt-task-brief>" in msg and len(msg) > len(brief_text):
                brief_text = msg
    if not brief_text:
        return []

    ranked: list[str] = []
    for raw in brief_text.splitlines():
        line = raw.strip()
        # Ranked lines look like "1. path/to/file.py (def ...)".
        if not line or "." not in line:
            continue
        head, _, rest = line.partition(".")
        if not head.isdigit():
            continue
        token = rest.strip()
        if not token:
            continue
        # Stop the path at the first space or '(' — the rest is signature info.
        path = token.split(" ")[0].split("(")[0].strip().rstrip(":")
        if path and "/" in path or path.endswith((".py", ".rst", ".md", ".txt",
                                                   ".go", ".js", ".ts", ".java",
                                                   ".rs", ".c", ".h", ".cpp")):
            ranked.append(_norm_path(path))
    return ranked


def compute_localization(instance: dict, gold_files: list[str]) -> dict[str, Any]:
    """Localization metrics from the brief's ranked file list (first match wins)."""
    ranked = _extract_brief_ranked_files(instance)
    gold = [_norm_path(g) for g in gold_files]

    first_gold_rank: Optional[int] = None
    for i, rf in enumerate(ranked, start=1):
        if any(_path_suffix_match(rf, g) for g in gold):
            first_gold_rank = i
            break

    def hit_at(k: int) -> Optional[bool]:
        if not ranked:
            return None
        return first_gold_rank is not None and first_gold_rank <= k

    top3 = ranked[:3]
    if top3:
        n_gold_in_top3 = sum(
            1 for rf in top3 if any(_path_suffix_match(rf, g) for g in gold)
        )
        focus_precision = n_gold_in_top3 / len(top3)
        focus_coverage = (
            n_gold_in_top3 / len(gold) if gold else None
        )
    else:
        focus_precision = None
        focus_coverage = None

    return {
        "brief_ranked_files": ranked,
        "first_gold_rank": first_gold_rank,
        "hit_at_1": hit_at(1),
        "hit_at_3": hit_at(3),
        "hit_at_5": hit_at(5),
        "focus_precision": focus_precision,
        "focus_coverage": focus_coverage,
    }


# ---------------------------------------------------------------------------
# RUN-LEVEL: navigation (1-based index among counted agent actions)
# ---------------------------------------------------------------------------
def compute_navigation(instance: dict, gold_files: list[str]) -> dict[str, Any]:
    history = instance.get("history") or []
    gold = [_norm_path(g) for g in gold_files]

    counted = 0
    error_count = 0
    first_edit_action: Optional[int] = None
    first_edit_file: Optional[str] = None
    reach_to_gold_action: Optional[int] = None
    edit_to_gold_action: Optional[int] = None

    for ev in history:
        if ev.get("observation") == "error":
            error_count += 1
        if not _is_counted_action(ev):
            continue
        counted += 1
        action = ev.get("action")
        path = _norm_path((ev.get("args") or {}).get("path") or "")

        # reach uses the SAME rigorous path-suffix matching as edit (below),
        # not a loose substring test — so "cat notimporter.py" does NOT count
        # as reaching gold "importer.py".
        if reach_to_gold_action is None and _action_reaches_gold(ev, gold):
            reach_to_gold_action = counted

        if action == "edit":
            if first_edit_action is None:
                first_edit_action = counted
                first_edit_file = path or None
            if edit_to_gold_action is None and any(
                _path_suffix_match(path, g) for g in gold
            ):
                edit_to_gold_action = counted

    return {
        "action_count": counted,
        "error_count": error_count,
        "first_edit_action": first_edit_action,
        "first_edit_file": first_edit_file,
        "reach_to_gold_action": reach_to_gold_action,
        "edit_to_gold_action": edit_to_gold_action,
        "gold_reached": reach_to_gold_action is not None,
        "gold_edited": edit_to_gold_action is not None,
        "pre_edit_nav_actions": (
            (first_edit_action - 1) if first_edit_action is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# RUN-LEVEL: outcome (final patch)
# ---------------------------------------------------------------------------
def _patch_files(patch_text: str) -> list[str]:
    """Files touched by a unified diff (reuses gold-patch parser)."""
    return [g["file"] for g in parse_gold_patch(patch_text)]


def compute_outcome(instance: dict, gold_files: list[str]) -> dict[str, Any]:
    history = instance.get("history") or []
    test_result = instance.get("test_result") or {}
    patch = ""
    if isinstance(test_result, dict):
        patch = test_result.get("git_patch") or ""
    gold = [_norm_path(g) for g in gold_files]

    files = sorted(set(_patch_files(patch)))
    touches_gold = any(
        any(_path_suffix_match(pf, g) for g in gold) for pf in files
    )
    return {
        "has_patch": bool(patch.strip()),
        "patch_files": files,
        "patch_touches_gold": touches_gold,
        "n_history_events": len(history),
    }


# ---------------------------------------------------------------------------
# REGRESSION GUARDS (run-level, must be 0 on clean runs)
# ---------------------------------------------------------------------------
def _count_boundary_corruption(text: str) -> int:
    """Count truncated/glued marker openers.

    A corruption is either:
      (a) a marker opener (e.g. '[CATCHE') immediately followed by an
          alphanumeric word with NO closing ']' before the next whitespace —
          i.e. a truncated opener glued onto a word; or
      (b) a 'wit#'-style mid-word glue: a '#' embedded inside an alphabetic
          run (letters on both sides), which indicates a marker char was
          mangled into the middle of a word.
    Deterministic structural scan — no regex over log bytes.
    """
    count = 0

    # (a) truncated/glued marker openers
    for opener in _MARKER_OPENERS:
        start = 0
        while True:
            idx = text.find(opener, start)
            if idx < 0:
                break
            start = idx + 1
            after = text[idx + len(opener):]
            # Look at the run up to the next whitespace.
            j = 0
            while j < len(after) and not after[j].isspace():
                j += 1
            chunk = after[:j]
            # A well-formed opener closes with ']' inside this whitespace-run
            # (e.g. "[SIGNATURE]") or the opener itself already ended at a
            # word boundary that is a space (handled: chunk empty).
            if "]" in chunk:
                continue  # properly closed -> not corruption
            # No ']' before whitespace. If the very next char is an alnum, the
            # opener is glued onto a word with no closing bracket -> corruption.
            if chunk and (chunk[0].isalnum() or chunk[0] in "_"):
                count += 1

    # (b) 'wit#'-style mid-word glue: '#' with a letter on each side.
    for i, ch in enumerate(text):
        if ch == "#" and 0 < i < len(text) - 1:
            if text[i - 1].isalpha() and text[i + 1].isalpha():
                count += 1

    return count


def _count_empty_contracts(text: str) -> int:
    """Count contract/guard lines whose post-colon value is empty/whitespace."""
    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        for label in _CONTRACT_LABELS:
            if not line.startswith(label):
                continue
            value = line[len(label):].strip()
            # '[BEHAVIORAL CONTRACT]' has no colon — empty when nothing follows.
            if label.endswith("]"):
                if not value:
                    count += 1
            else:
                # label ends with ':'. Empty when nothing meaningful follows.
                if not value:
                    count += 1
            break
    return count


def compute_regression_guards(visible_blobs: list[str]) -> dict[str, int]:
    boundary = 0
    empty = 0
    for blob in visible_blobs:
        boundary += _count_boundary_corruption(blob)
        empty += _count_empty_contracts(blob)
    return {
        "boundary_corruption_count": boundary,
        "empty_contract_count": empty,
    }


# ---------------------------------------------------------------------------
# PER-LAYER METRICS
# ---------------------------------------------------------------------------
def _gold_func_names(gold_patch: str) -> set[str]:
    if not gold_patch:
        return set()
    funcs: set[str] = set()
    for g in parse_gold_patch(gold_patch):
        for fn in g.get("functions", []):
            funcs.add(fn)
    return funcs


def _content_on_gold(text: str, gold_files: list[str], gold_funcs: set[str]) -> bool:
    """Does this layer's delivered text reference a gold file or function?

    Path-suffix match on any gold file basename, or a gold function name as a
    whole-ish token. Reuses evidence_scorer-style matching (suffix + name).
    """
    norm = text.replace("\\", "/")
    for base in _gold_basenames(gold_files):
        if base and base in norm:
            return True
    for fn in gold_funcs:
        if fn and fn in text:
            return True
    return False


def _layer_uptake(
    layer_files: set[str],
    layer_funcs: set[str],
    later_actions: list[dict],
) -> bool:
    """True if a LATER counted agent action references a layer file/function.

    Adapts evidence_scorer.compute_uptake to the post-injection action slice:
    we build one InjectionRecord per delivered file/function and reuse that
    module's reference logic implicitly (basename/function substring match
    over the action target text).
    """
    action_text = " ".join(
        _action_target_text(a).lower() for a in later_actions
    )
    # also include thoughts/new_str of edits so a function written into code counts
    for a in later_actions:
        args = a.get("args") or {}
        for k in ("new_str", "content", "file_text", "thought"):
            v = args.get(k)
            if isinstance(v, str) and v:
                action_text += " " + v.lower()

    for f in layer_files:
        base = _norm_path(f).split("/")[-1].lower()
        if base and base in action_text:
            return True
    for fn in layer_funcs:
        if fn and fn.lower() in action_text:
            return True
    return False


def _extract_paths(text: str) -> set[str]:
    """Extract file-path-looking tokens from delivered text (structural split)."""
    paths: set[str] = set()
    for token in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
        t = token.strip().strip('"').strip("'").rstrip(":>").lstrip("<")
        t = t.replace("\\", "/")
        if "/" in t and t.endswith((".py", ".rst", ".md", ".go", ".js", ".ts",
                                    ".java", ".rs", ".c", ".h", ".cpp", ".txt")):
            paths.add(t)
        elif ":" in t:
            # "beets/dbcore/db.py:722" form
            head = t.split(":")[0]
            if "/" in head and head.endswith((".py", ".rst", ".md", ".go",
                                              ".js", ".ts", ".java", ".rs")):
                paths.add(head)
    return paths


# Grep/search subcommands that take a PATTERN then a FILE argument.
_GREP_LIKE = ("grep", "rg", "ag", "ack")
# Flags that consume no following file token (so we don't mistake them for one).
_GREP_FLAG_PREFIX = "-"


def _grep_symbol_and_file(cmd: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (symbol, grepped_file) from a grep/sed-style command.

    Returns the searched SYMBOL and the SPECIFIC FILE the command targets, but
    ONLY when the command names a concrete file (a token ending in a source
    extension). When the command searches a directory, has no file argument, or
    has no isolable symbol, returns ``(None, None)`` — correct-or-quiet, so a
    homonym is never asserted on an undeterminable command.

    Examples:
      grep -n "set_fields" beets/importer.py  -> ("set_fields", "beets/importer.py")
      sed -n '600,620p' beets/importer.py     -> (None, "beets/importer.py")  # no symbol
      grep -rn "x" beets/                      -> (None, None)                  # dir, not file
    """
    if not cmd:
        return (None, None)
    norm = _norm_path(cmd)
    # A specific file target: a single file token. If 0 or >1 distinct file
    # tokens, we cannot pin "the grepped file" with certainty.
    files = _file_tokens(norm)
    grepped_file = next(iter(files)) if len(files) == 1 else None

    # Symbol: the search PATTERN of a grep-like subcommand. We only trust a
    # clean single-identifier pattern (letters/digits/underscore, optionally
    # prefixed by "def "). Alternations ("a\\|b"), regex metachars, or sed
    # range patterns yield no symbol.
    symbol: Optional[str] = None
    toks = norm.split()
    for i, tok in enumerate(toks):
        base = tok.rsplit("/", 1)[-1]
        if base in _GREP_LIKE:
            # Walk forward to the first non-flag token = the pattern.
            for nxt in toks[i + 1:]:
                if nxt.startswith(_GREP_FLAG_PREFIX):
                    continue
                pat = nxt.strip().strip('"').strip("'").strip("`")
                # Drop a leading "def " that may be glued inside quotes.
                if pat.startswith("def "):
                    pat = pat[len("def "):].strip()
                # Accept ONLY a clean identifier (no regex metachars / alts).
                ident = pat.replace("def ", "").strip()
                if ident and all(c.isalnum() or c == "_" for c in ident) \
                        and not ident.isdigit():
                    symbol = ident
                break
            break
    return (symbol, grepped_file)


def _block_names_symbol_defined_elsewhere(
    blob: str, symbol: str, grepped_file: str
) -> bool:
    """True iff the GT block asserts SYMBOL is DEFINED in a file != grepped_file.

    This is the #44 homonym defect: the agent greps a symbol in one file and GT
    surfaces the SAME symbol name as defined in a DIFFERENT file. We require an
    explicit DEFINITION assertion of that exact symbol — not a caller listing.

    Two definition forms are recognized:
      1. ``<symbol> defined at|in <path>``  (e.g. "set_fields defined at zero.py")
      2. ``<path>::<symbol>``               (e.g. "zero.py::set_fields")

    The ``[GT] Called by:`` form lists ``caller_file::caller_func`` where the
    token after ``::`` is the CALLER's name, not the grepped symbol — so it does
    NOT trigger unless the caller function happens to share the grepped symbol's
    name AND lives in a different file (a genuine homonym).
    """
    if not symbol or not grepped_file:
        return False
    norm = blob.replace("\\", "/")

    # Form 2: "<path>::<symbol>" where the symbol equals the grepped symbol and
    # the path's file differs from the grepped file.
    needle = "::" + symbol
    start = 0
    while True:
        idx = norm.find(needle, start)
        if idx < 0:
            break
        start = idx + len(needle)
        # The char after the symbol must be a non-identifier (word boundary).
        after = norm[idx + len(needle): idx + len(needle) + 1]
        if after and (after.isalnum() or after == "_"):
            continue  # "::set_fields_extra" — different symbol
        # Walk left from '::' to capture the file path token.
        left = norm[:idx]
        j = len(left)
        while j > 0 and (left[j - 1].isalnum() or left[j - 1] in "._/-"):
            j -= 1
        ref_path = left[j:]
        if ref_path.endswith(_SOURCE_EXTS) and not _path_suffix_match(
            ref_path, grepped_file
        ) and not _path_suffix_match(grepped_file, ref_path):
            return True

    # Form 1: "<symbol> defined at|in <path>".
    for marker in (symbol + " defined at ", symbol + " defined in "):
        idx = norm.find(marker)
        if idx < 0:
            continue
        tail = norm[idx + len(marker):]
        ref = tail.split()[0] if tail.split() else ""
        ref = ref.strip().strip('"').strip("'").rstrip(":,.")
        if ":" in ref:
            head, _, num = ref.rpartition(":")
            if head and num.isdigit():
                ref = head
        if ref.endswith(_SOURCE_EXTS) and not _path_suffix_match(
            ref, grepped_file
        ) and not _path_suffix_match(grepped_file, ref):
            return True

    return False


def _layer_fired_counts(gt_log: Optional[str]) -> dict[str, int]:
    """Per-layer 'fired' counts from a gt_log JSONL (layer field). None-safe.

    Reuses evidence_scorer.parse_injections to read the log; each record's
    ``layer`` field is normalized against our layer keys by case-insensitive
    substring (so 'L3', 'l3_postedit', 'post_edit' all map sensibly).
    """
    counts: dict[str, int] = {}
    if not gt_log or not os.path.exists(gt_log):
        return counts
    records = parse_injections(gt_log)
    alias = {
        "l1": "L1_brief", "brief": "L1_brief",
        "l3b": "L3b_similar", "similar": "L3b_similar",
        "l3": "L3_postedit", "post_edit": "L3_postedit", "postedit": "L3_postedit",
        "post_view": "postview", "postview": "postview",
        "l5": "L5_advisory", "advisory": "L5_advisory",
        "l6": "L6_verify", "verify": "L6_verify",
        "grep": "grep_intercept",
        "curation": "curation",
    }
    for rec in records:
        lay = (rec.layer or "").strip().lower()
        mapped = None
        # exact key
        for k in LAYER_MARKERS:
            if lay == k.lower():
                mapped = k
                break
        if mapped is None:
            for a, k in alias.items():
                if a in lay:
                    mapped = k
                    break
        if mapped:
            counts[mapped] = counts.get(mapped, 0) + 1
    return counts


def compute_per_layer(
    instance: dict,
    gold_files: list[str],
    *,
    gt_log: Optional[str] = None,
    gold_patch: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    history = instance.get("history") or []
    gold_funcs = _gold_func_names(gold_patch or "")
    fired = _layer_fired_counts(gt_log)

    # Pre-index counted actions with their history position so we can build the
    # "later actions" slice for uptake.
    counted_actions: list[tuple[int, dict]] = []
    for hpos, ev in enumerate(history):
        if _is_counted_action(ev):
            counted_actions.append((hpos, ev))

    per_layer: dict[str, dict[str, Any]] = {}

    for layer, markers in LAYER_MARKERS.items():
        delivered_events: list[tuple[int, str]] = []  # (history_pos, text)
        for hpos, ev in enumerate(history):
            if not _is_agent_visible(ev):
                continue
            for blob in _visible_texts(ev):
                if any(m in blob for m in markers):
                    # grep_intercept: only the substantive "[GT] Called by:" form,
                    # OR a generic "[GT] " that is NOT brief/postedit/postview/etc.
                    if layer == "grep_intercept":
                        if _GREP_INTERCEPT_HINT not in blob:
                            # generic [GT] — exclude if it's actually another layer
                            if any(
                                other_m in blob
                                for ol, oms in LAYER_MARKERS.items()
                                if ol != "grep_intercept"
                                for other_m in oms
                            ):
                                continue
                    delivered_events.append((hpos, blob))
                    break  # one hit per event per layer

        delivered = len(delivered_events) > 0
        delivered_count = len(delivered_events)
        delivered_chars = sum(len(t) for _, t in delivered_events)
        all_text = "\n".join(t for _, t in delivered_events)

        on_gold = (
            _content_on_gold(all_text, gold_files, gold_funcs)
            if delivered else False
        )

        # uptake: later counted action references a layer file/function.
        layer_files = set()
        for _, t in delivered_events:
            layer_files |= _extract_paths(t)
        layer_funcs = set(gold_funcs) & set(  # function tokens that appear in the layer text
            fn for fn in gold_funcs if fn and fn in all_text
        )
        uptake = False
        if delivered_events:
            first_pos = min(hpos for hpos, _ in delivered_events)
            later = [ev for hpos, ev in counted_actions if hpos > first_pos]
            uptake = _layer_uptake(layer_files, layer_funcs, later)

        entry: dict[str, Any] = {
            "delivered": delivered,
            "delivered_count": delivered_count,
            "delivered_chars": delivered_chars,
            "on_gold": on_gold,
            "uptake": uptake,
            "fired": fired.get(layer) if gt_log else None,
        }

        # ---- layer-specific fields ----
        if layer == "L1_brief":
            loc = compute_localization(instance, gold_files)
            entry["first_gold_rank"] = loc["first_gold_rank"]
            entry["edit_target_contracts_delivered"] = any(
                _EDIT_TARGET_CONTRACTS in t for _, t in delivered_events
            ) or any(
                _EDIT_TARGET_CONTRACTS in blob
                for ev in history if _is_agent_visible(ev)
                for blob in _visible_texts(ev)
            )
        elif layer == "L3_postedit":
            entry["contract_delivered"] = any(
                ("[BEHAVIORAL CONTRACT]" in t) or ("[CONTRACT]" in t)
                for _, t in delivered_events
            )
            entry["callee_sig_delivered"] = any(
                ("[SIGNATURE]" in t) or ("Calls into:" in t)
                for _, t in delivered_events
            )
            entry["callers_delivered"] = any(
                ("Called by:" in t) or ("Calls into:" in t)
                for _, t in delivered_events
            )
        elif layer == "L3b_similar":
            entry["twin_delivered"] = any(
                any(tm in t for tm in _TWIN_MARKERS) for _, t in delivered_events
            )
            entry["similar_count"] = sum(
                1 for _, t in delivered_events
                if any(sm in t for sm in _SIMILAR_MARKERS)
            )
        elif layer == "L5_advisory":
            entry["fired_count"] = delivered_count
        elif layer == "L6_verify":
            entry["test_targets_delivered"] = delivered
        elif layer == "grep_intercept":
            # cross_file_ref_count (loose, REAL signal): every delivered path
            # that differs from the grepped file. These are legitimate
            # cross-file callers/refs, NOT name-homonyms.
            #
            # homonym_count (#44 defect, correct-or-quiet): fires ONLY when the
            # grepped command names a SPECIFIC file AND a SPECIFIC symbol, and
            # the GT block asserts that SAME symbol as DEFINED in a DIFFERENT
            # file. When the symbol/file is undeterminable, we stay quiet (do
            # not increment) rather than overstate.
            cross_file_refs = 0
            homonyms = 0
            for hpos, t in delivered_events:
                delivered_paths = _extract_paths(t)
                # the grepped command is the nearest preceding counted action.
                grepped_cmd = ""
                for ap, aev in reversed(counted_actions):
                    if ap <= hpos:
                        grepped_cmd = ((aev.get("args") or {}).get("command")
                                       or _action_target_text(aev))
                        break
                grepped_norm = _norm_path(grepped_cmd)
                for dp in delivered_paths:
                    if grepped_norm and not _path_suffix_match(dp, grepped_norm) \
                            and not _path_suffix_match(grepped_norm, dp):
                        cross_file_refs += 1
                # true homonym: same symbol, different DEFINING file.
                symbol, grepped_file = _grep_symbol_and_file(grepped_cmd)
                if symbol and grepped_file and \
                        _block_names_symbol_defined_elsewhere(
                            t, symbol, grepped_file):
                    homonyms += 1
            entry["cross_file_ref_count"] = cross_file_refs
            entry["homonym_count"] = homonyms
        elif layer == "curation":
            # curation MUST be absent from agent obs — presence is a leak.
            entry["delivered"] = delivered  # already set; explicit for clarity

        per_layer[layer] = entry

    return per_layer


# ---------------------------------------------------------------------------
# TOP-LEVEL
# ---------------------------------------------------------------------------
def compute_run_metrics(
    output_jsonl_path: str,
    gold_files: list[str],
    *,
    gt_log: Optional[str] = None,
    gold_patch: Optional[str] = None,
) -> dict[str, Any]:
    """Compute the full run-level + per-layer metric dict for one trajectory."""
    instance = load_instance(output_jsonl_path)
    gold_files = list(gold_files or [])

    # gold_patch may be a path or inline text; read if it's an existing file.
    gp_text = gold_patch or ""
    if gold_patch and os.path.exists(gold_patch):
        with open(gold_patch, encoding="utf-8", errors="ignore") as f:
            gp_text = f.read()

    history = instance.get("history") or []
    visible_blobs: list[str] = []
    for ev in history:
        if _is_agent_visible(ev):
            visible_blobs.extend(_visible_texts(ev))

    localization = compute_localization(instance, gold_files)
    navigation = compute_navigation(instance, gold_files)
    outcome = compute_outcome(instance, gold_files)
    guards = compute_regression_guards(visible_blobs)
    per_layer = compute_per_layer(
        instance, gold_files, gt_log=gt_log, gold_patch=gp_text
    )

    metrics = {
        "instance_id": instance.get("instance_id"),
        "gold_files": [_norm_path(g) for g in gold_files],
        "brief_delivered": per_layer.get("L1_brief", {}).get("delivered", False),
        "localization": localization,
        "navigation": navigation,
        "outcome": outcome,
        "regression_guards": guards,
        "per_layer": per_layer,
    }
    # Added view (NOT a restructure): regroup the flat keys above into a
    # two-sided lens — did GT PRODUCE+DELIVER correct context (gt_side), and did
    # the agent RECEIVE+ACT on it (agent_side). All flat keys are preserved.
    metrics["two_sided"] = two_sided_view(metrics)
    return metrics


# Per-layer delivery-flag fields that belong to the GT (production) side.
_GT_SIDE_LAYER_FIELDS = (
    "delivered", "delivered_count", "delivered_chars", "on_gold",
    "edit_target_contracts_delivered", "twin_delivered", "callee_sig_delivered",
    "contract_delivered", "callers_delivered", "test_targets_delivered",
    "similar_count", "first_gold_rank", "fired", "fired_count",
)
# Per-layer fields that belong to the agent (reception/behavior) side.
_AGENT_SIDE_LAYER_FIELDS = ("uptake",)


def two_sided_view(metrics: dict[str, Any]) -> dict[str, Any]:
    """Regroup computed metrics into ``gt_side`` and ``agent_side`` lenses.

    This is an ADDED view over the existing flat keys, not a restructure.

      * ``gt_side`` answers "did GT produce + deliver CORRECT context?" —
        localization (hit_at_k, first_gold_rank, focus_*), per-layer delivery
        flags + sizes + on_gold, and the regression guards (boundary
        corruption, empty contracts, curation leak, homonym).
      * ``agent_side`` answers "did the agent receive + ACT on it?" —
        navigation (reach/edit/first-edit, action/error counts, gold
        reached/edited), per-layer uptake, and the final outcome (has_patch,
        patch_touches_gold). Paired deltas are carried through when present.

    Deterministic: sub-dicts are built in a fixed key order from a fixed field
    list; no I/O, no randomness.
    """
    loc = metrics.get("localization", {}) or {}
    nav = metrics.get("navigation", {}) or {}
    out = metrics.get("outcome", {}) or {}
    guards = metrics.get("regression_guards", {}) or {}
    per_layer = metrics.get("per_layer", {}) or {}

    grep = per_layer.get("grep_intercept", {}) or {}
    curation = per_layer.get("curation", {}) or {}

    # ---- gt_side ----
    gt_layers: dict[str, dict[str, Any]] = {}
    agent_layers: dict[str, dict[str, Any]] = {}
    for layer, entry in per_layer.items():
        gt_layers[layer] = {
            k: entry[k] for k in _GT_SIDE_LAYER_FIELDS if k in entry
        }
        agent_layers[layer] = {
            k: entry[k] for k in _AGENT_SIDE_LAYER_FIELDS if k in entry
        }

    gt_side: dict[str, Any] = {
        # localization (was GT produce the right ranking)
        "hit_at_1": loc.get("hit_at_1"),
        "hit_at_3": loc.get("hit_at_3"),
        "hit_at_5": loc.get("hit_at_5"),
        "first_gold_rank": loc.get("first_gold_rank"),
        "focus_precision": loc.get("focus_precision"),
        "focus_coverage": loc.get("focus_coverage"),
        "brief_delivered": metrics.get("brief_delivered"),
        # regression guards (did GT deliver clean, non-corrupt, non-leaking context)
        "boundary_corruption_count": guards.get("boundary_corruption_count"),
        "empty_contract_count": guards.get("empty_contract_count"),
        "curation_leak": bool(curation.get("delivered")),
        "homonym_count": grep.get("homonym_count"),
        "cross_file_ref_count": grep.get("cross_file_ref_count"),
        # per-layer delivery view
        "per_layer": gt_layers,
    }

    # ---- agent_side ----
    agent_side: dict[str, Any] = {
        # navigation (did the agent reach/edit gold, how much wandering)
        "reach_to_gold_action": nav.get("reach_to_gold_action"),
        "edit_to_gold_action": nav.get("edit_to_gold_action"),
        "first_edit_action": nav.get("first_edit_action"),
        "action_count": nav.get("action_count"),
        "error_count": nav.get("error_count"),
        "pre_edit_nav_actions": nav.get("pre_edit_nav_actions"),
        "gold_reached": nav.get("gold_reached"),
        "gold_edited": nav.get("gold_edited"),
        # outcome (did the agent submit a patch that touches gold)
        "has_patch": out.get("has_patch"),
        "patch_touches_gold": out.get("patch_touches_gold"),
        "n_history_events": out.get("n_history_events"),
        # per-layer uptake view
        "per_layer": agent_layers,
    }
    # Carry paired deltas through to the agent side when present (behavioral).
    if "paired_deltas" in metrics:
        agent_side["paired_deltas"] = metrics["paired_deltas"]

    return {"gt_side": gt_side, "agent_side": agent_side}


def emit_run_metrics(
    output_jsonl_path: str,
    gold_files: list[str],
    out_path: str,
    **kw: Any,
) -> dict[str, Any]:
    """Compute metrics and write them to ``out_path`` as JSON. Returns the dict."""
    metrics = compute_run_metrics(output_jsonl_path, gold_files, **kw)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, sort_keys=True)
    return metrics


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def paired_deltas(
    metrics_gt: dict[str, Any], metrics_baseline: dict[str, Any]
) -> dict[str, Any]:
    """Numeric deltas (gt - baseline) for shared, numeric metrics.

    Deterministic: keys are sorted; only metrics numeric in BOTH runs produce a
    delta. Booleans and nulls are skipped (they are not numeric deltas).
    """
    deltas: dict[str, Any] = {}

    def section(name: str) -> None:
        g = metrics_gt.get(name, {}) or {}
        b = metrics_baseline.get(name, {}) or {}
        for k in sorted(set(g) | set(b)):
            gv, bv = _num(g.get(k)), _num(b.get(k))
            if gv is not None and bv is not None:
                deltas[f"{name}.{k}"] = gv - bv

    section("localization")
    section("navigation")
    section("outcome")
    section("regression_guards")

    # per-layer numeric deltas
    gpl = metrics_gt.get("per_layer", {}) or {}
    bpl = metrics_baseline.get("per_layer", {}) or {}
    for layer in sorted(set(gpl) | set(bpl)):
        gl = gpl.get(layer, {}) or {}
        bl = bpl.get(layer, {}) or {}
        for k in sorted(set(gl) | set(bl)):
            gv, bv = _num(gl.get(k)), _num(bl.get(k))
            if gv is not None and bv is not None:
                deltas[f"per_layer.{layer}.{k}"] = gv - bv

    return deltas


# ---------------------------------------------------------------------------
# CLI / pretty printer
# ---------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, list):
        return f"[{len(v)} items]"
    return str(v)


def _print_section(title: str, d: dict[str, Any]) -> None:
    print(f"\n== {title} ==")
    for k in sorted(d):
        print(f"  {k:28s} {_fmt(d[k])}")


def _print_per_layer(per_layer: dict[str, dict[str, Any]]) -> None:
    print("\n== per-layer ==")
    for layer in LAYER_MARKERS:  # stable, documented order
        entry = per_layer.get(layer, {})
        if not entry:
            continue
        common = (
            f"delivered={_fmt(entry.get('delivered'))} "
            f"count={_fmt(entry.get('delivered_count'))} "
            f"chars={_fmt(entry.get('delivered_chars'))} "
            f"on_gold={_fmt(entry.get('on_gold'))} "
            f"uptake={_fmt(entry.get('uptake'))} "
            f"fired={_fmt(entry.get('fired'))}"
        )
        print(f"  {layer:14s} {common}")
        extras = {
            k: v for k, v in entry.items()
            if k not in ("delivered", "delivered_count", "delivered_chars",
                         "on_gold", "uptake", "fired")
        }
        if extras:
            extra_str = "  ".join(f"{k}={_fmt(v)}" for k, v in sorted(extras.items()))
            print(f"                 {extra_str}")


def _print_side(title: str, side: dict[str, Any]) -> None:
    """Print one two-sided lens: scalar fields, then per-layer sub-dict."""
    print(f"\n{title}")
    scalars = {k: v for k, v in side.items() if k != "per_layer"}
    for k in sorted(scalars):
        print(f"  {k:28s} {_fmt(scalars[k])}")
    per_layer = side.get("per_layer", {}) or {}
    if per_layer:
        print("  per-layer:")
        for layer in LAYER_MARKERS:  # stable, documented order
            entry = per_layer.get(layer)
            if not entry:
                continue
            fields = "  ".join(
                f"{k}={_fmt(v)}" for k, v in sorted(entry.items())
            )
            if fields:
                print(f"    {layer:14s} {fields}")


def _print_two_sided(metrics: dict[str, Any]) -> None:
    ts = metrics.get("two_sided", {}) or {}
    if not ts:
        return
    _print_side("=== GT SIDE (production/delivery) ===", ts.get("gt_side", {}))
    _print_side("=== AGENT SIDE (reception/behavior) ===", ts.get("agent_side", {}))


def _print_table(metrics: dict[str, Any]) -> None:
    print(f"instance_id: {metrics.get('instance_id')}")
    print(f"gold_files:  {', '.join(metrics.get('gold_files', [])) or '-'}")
    print(f"brief_delivered: {_fmt(metrics.get('brief_delivered'))}")
    _print_section("localization", metrics.get("localization", {}))
    _print_section("navigation", metrics.get("navigation", {}))
    _print_section("outcome", metrics.get("outcome", {}))
    _print_section("regression_guards", metrics.get("regression_guards", {}))
    _print_per_layer(metrics.get("per_layer", {}))
    _print_two_sided(metrics)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m groundtruth.metrics.run_metrics",
        description="Trajectory-derived per-run + per-layer metrics for GroundTruth.",
    )
    parser.add_argument("--output", required=True, help="path to output.jsonl")
    parser.add_argument(
        "--gold-files", required=True,
        help="comma-separated gold file paths (e.g. a.py,b.py)",
    )
    parser.add_argument("--gold-patch", default=None, help="path to gold patch .diff")
    parser.add_argument("--baseline", default=None, help="baseline output.jsonl for deltas")
    parser.add_argument("--gt-log", default=None, help="gt_interactions.jsonl for 'fired' counts")
    parser.add_argument("--out", default="run_metrics.json", help="output json path")
    args = parser.parse_args(argv)

    gold_files = [g.strip() for g in args.gold_files.split(",") if g.strip()]

    metrics = emit_run_metrics(
        args.output, gold_files, args.out,
        gt_log=args.gt_log, gold_patch=args.gold_patch,
    )
    _print_table(metrics)
    print(f"\nwrote {args.out}")

    if args.baseline:
        baseline = compute_run_metrics(
            args.baseline, gold_files,
            gt_log=args.gt_log, gold_patch=args.gold_patch,
        )
        deltas = paired_deltas(metrics, baseline)
        print("\n== paired deltas (gt - baseline) ==")
        for k in sorted(deltas):
            print(f"  {k:40s} {deltas[k]:+.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
