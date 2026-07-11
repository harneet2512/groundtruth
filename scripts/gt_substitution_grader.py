#!/usr/bin/env python3
"""gt_substitution_grader.py — THE INSTRUMENT (W0) for the acquisition-layer thesis.

Doctrine (gt_gt.md §24.2/§24.4): GT is the deterministic ACQUISITION LAYER. The
model can find everything; it pays for every fact in TURNS + WINDOW. GT's job is to
SUBSTITUTE the acquisition the model would otherwise perform. The TRUE consumption
signal is therefore NON-RE-ACQUISITION (RQ2): if GT delivered "def of X: a/b.py:120"
and the agent LATER performs the acquisition that fact answers (greps X, hunts the
file), delivery FAILED regardless of telemetry; if the search VANISHES, the fact
substituted. This grader reads a run's trajectory + delivered payloads and measures
that negative space — deterministically, from the agent's own actions.

This is a MEASUREMENT-ONLY reader. It edits nothing, calls no LLM, hits no network,
and writes only under an explicit --out dir (never inside the repo).

Two trajectory shapes are supported (shape-aware). The shape-detection + parsing
below is a small LOCAL MIRROR of the readers in scripts/swebench/gt_deep_metrics.py
(``_from_miniswe_trajectory`` / ``_find_miniswe_trajectory`` — pier truncated dirs)
and scripts/measure_brief.py (``_iter_agent_commands`` / ``_canonical_trajectory``).
It imports nothing private from them (keeps this instrument decoupled); the mirror is
intentional and noted so a future reader knows where the canonical logic lives.

Metrics per run (8-decimal JSON):
  * delivered_fact_count per class {localization-file, symbol-definition, caller-fact,
    verify_nudge_file (legacy output name: covering-test), obligation}
  * re_acquisition_events / re_acquired_fact_count / non_re_acquisition_rate
  * turns_to_first_edit, turns_to_first_gold_read (needs --gold), turns_to_first_test_run
  * wrong_branch_reads (reads of files never edited nor named by any delivered fact)
  * receipt ladder levels 1-4 per fact class (W4; see RECEIPT_LADDER_NOTE). Every
    single-arm number is labeled ``arms=single`` + ``causal=UNAVAILABLE(single-arm)``:
    level-5 CAUSAL is PAIRED-ONLY (B-7 / gate item 12) and is emitted ONLY by
    ``compute_causal(on_report, off_report)`` over a matched GT-on/GT-off pair; a single
    arm is REFUSED (``SingleArmCausalError``) through ``assert_paired_for_causal``.

Usage:
  gt_substitution_grader.py <run_dir> [--gold a.py --gold b.py] [--out FILE]
  gt_substitution_grader.py --glob "D:/gt_runs/**" --out-dir D:/gt_runs/instrument_.../
  gt_substitution_grader.py --pair-on ON_DIR --pair-off OFF_DIR [--gold ...] [--out FILE]
                       # B-7: paired-only CAUSAL (level 5) over a matched GT-on/GT-off pair
  gt_substitution_grader.py --receipt-table --corpus name=ROOT [--corpus ...] \
      --out-dir DIR    # F9: the reproducible consumption-table driver (provenance
                       # recorded in AGG_COMBINED.json + PROVENANCE.json)
"""
from __future__ import annotations

import argparse
import glob as _glob
import importlib.util as _ilu
import json
import os
import re
import statistics
import sys
from collections import Counter

# ---------------------------------------------------------------------------
# RECEIPT LADDER vocabulary (W4). Single source of truth is
# src/groundtruth/runtime/evidence_envelope.py (TITO law 7). Import its RECEIPT_*
# constants when the module is importable; else MIRROR the exact strings so the
# grader stays decoupled + runnable standalone.
# ---------------------------------------------------------------------------
def _load_receipt_constants() -> dict:
    ee = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "src", "groundtruth", "runtime", "evidence_envelope.py")
    try:
        name = "_gt_evidence_envelope_receipts"
        spec = _ilu.spec_from_file_location(name, ee)
        if spec and spec.loader:
            mod = _ilu.module_from_spec(spec)
            # register BEFORE exec so the module's @dataclass decorators can resolve
            # their own module via sys.modules (module_from_spec gotcha).
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
                return {k: getattr(mod, k) for k in (
                    "RECEIPT_DELIVERED", "RECEIPT_REFERENCED", "RECEIPT_ACTED",
                    "RECEIPT_RESOLVED_STATE", "RECEIPT_CAUSAL")}
            finally:
                sys.modules.pop(name, None)
    except Exception:  # pragma: no cover - fall through to the mirror
        pass
    return {"RECEIPT_DELIVERED": "delivered", "RECEIPT_REFERENCED": "referenced",
            "RECEIPT_ACTED": "acted", "RECEIPT_RESOLVED_STATE": "resolved_state",
            "RECEIPT_CAUSAL": "causal"}


_RC = _load_receipt_constants()
RECEIPT_DELIVERED = _RC["RECEIPT_DELIVERED"]
RECEIPT_REFERENCED = _RC["RECEIPT_REFERENCED"]
RECEIPT_ACTED = _RC["RECEIPT_ACTED"]
RECEIPT_RESOLVED_STATE = _RC["RECEIPT_RESOLVED_STATE"]
RECEIPT_CAUSAL = _RC["RECEIPT_CAUSAL"]
# ordered ladder levels 1..4; level 5 CAUSAL is paired-only (never single-arm).
_RECEIPT_ORDER = (RECEIPT_DELIVERED, RECEIPT_REFERENCED, RECEIPT_ACTED,
                  RECEIPT_RESOLVED_STATE)
_RECEIPT_NUM = {name: i + 1 for i, name in enumerate(_RECEIPT_ORDER)}
RECEIPT_LADDER_NOTE = (
    "receipt ladder (evidence_envelope.py law 7): 1 delivered / 2 referenced / "
    "3 acted / 4 resolved_state. Level 5 CAUSAL is PAIRED-ONLY: documented here, "
    "NEVER computed single-arm. Detectors use word-boundary identifier tokenization "
    "+ path-aware (full-relpath) file comparison; basename fallback only when the "
    "basename is unambiguous among delivered file facts. "
    "CLASS verify_nudge_file (renamed from covering-test): <gt-verify> payloads are "
    "verify-NUDGE mentions of EDITED PRODUCTION FILES (test identifiers leak-scrubbed "
    "by design), so the class measures 'post-nudge: the agent ran a test NAMING the "
    "delivered file and it went green' — NOT executed-RED covering-test consumption "
    "(that needs the delivery seam's own receipt_state, W2, when live); its ACTED~1.0 "
    "is tautological (the token is a file the agent was editing); L4 has TWO columns: "
    "resolved_state = STRICT (decision-grade: a later green test run NAMES the "
    "delivered file, left word boundary) and resolved_state_any_green = WEAK (any "
    "later green test, target-blind). "
    "CLASS obligation (prose): delivered, and REFERENCED is a LOWER BOUND (a delivered "
    "code entity named by the obligation recurs in a later agent message); the "
    "code_shaped_heuristic middle (dunder/underscore/camelCase tokens, HEURISTIC) and "
    "the loose_ceiling (any shared identifier token) are reported alongside as bounds; "
    "acted/resolved_state are not deterministically attributable to prose (stay quiet). "
    "CLASS localization-file: reported RAW and DEDUPED (suffix-identity variant groups; "
    "group receipt = max over variants); ADMIT/CUT decisions cite the DEDUPED column.")

# ---------------------------------------------------------------------------
# ARMS + the CAUSAL (level-5) REFUSAL (B-7 / verifyandobserve.md §0 gate item 12).
# The receipt ladder has FIVE levels; a SINGLE arm (one run's trajectory) may substantiate
# AT MOST level 4 RESOLVED_STATE. Level 5 CAUSAL is a PAIRED / fixed-history counterfactual
# claim ("CAUSAL only from a paired / fixed-history counterfactual") and MUST NEVER be
# emitted from one arm: a one-arm action heuristic / receipt promotion is CORRELATION, not
# the doctrine's paired non-reacquisition / causal criterion. These are the enforcement
# primitives — the grader labels every single-arm number ``arms=single`` + ``causal=
# UNAVAILABLE(single-arm)`` so no consumer can read a single-arm RESOLVED_STATE rate as
# effectiveness, and routes every would-be causal emission through the chokepoint below.
# ---------------------------------------------------------------------------
ARMS_SINGLE = "single"
ARMS_PAIRED = "paired"
CAUSAL_UNAVAILABLE_SINGLE_ARM = "UNAVAILABLE(single-arm)"


class SingleArmCausalError(RuntimeError):
    """Raised when a CAUSAL (level-5) verdict is requested from anything that is not a
    matched GT-on/GT-off pair. Fail-loud on purpose: a causal claim built from one
    trajectory is a correlation masquerading as a counterfactual, and shipping it silently
    is exactly the B-7 defect (one-turn receipt heuristics read as effectiveness)."""


def assert_paired_for_causal(arms: str) -> None:
    """THE CHOKEPOINT — every code path that would emit a CAUSAL verdict MUST pass through
    here first. Raises :class:`SingleArmCausalError` unless ``arms`` is exactly
    :data:`ARMS_PAIRED`. There is no override: CAUSAL is paired-only (gate item 12)."""
    if arms != ARMS_PAIRED:
        raise SingleArmCausalError(
            "CAUSAL (receipt level 5) is paired-only (verifyandobserve.md §0 gate item "
            f"12): refusing to emit a causal verdict from arms={arms!r}. Supply a matched "
            "GT-on/GT-off pair (same task, same history prefix) via compute_causal().")


def cap_single_arm_level(level_num: int) -> int:
    """Cap a per-fact receipt level to at most RESOLVED_STATE (4) for single-arm grading —
    the explicit, testable enforcement of the level-5 refusal at the per-fact granularity:
    whatever a detector computes, a single trajectory can never report CAUSAL (5)."""
    return min(int(level_num), _RECEIPT_NUM[RECEIPT_RESOLVED_STATE])


# ---------------------------------------------------------------------------
# LOGIC (LIPI) — token-matching false-positive control.
# A delivered SYMBOL fact is only matchable as a re-acquisition needle when it is a
# whole word of length >= 3 (a 2-char symbol like "id" would match everywhere) AND
# not a bare language keyword (which appears as a node "name" occasionally but is
# never a useful acquisition target). This is the documented FP guard.
# ---------------------------------------------------------------------------
_MIN_SYMBOL_LEN = 3
_KEYWORD_STOP = {
    # cross-language reserved words only — NEVER real identifiers (we do not stoplist
    # real symbols like main/init/get; length + word-boundary carry those).
    "def", "for", "the", "and", "not", "let", "var", "new", "try", "else", "elif",
    "func", "type", "case", "with", "from", "import", "class", "return", "yield",
    "async", "await", "while", "break", "pass", "true", "false", "none", "null",
    "nil", "self", "this", "const", "then", "when", "goto", "int", "str",
}

# Source-file extensions (multi-language). Used for BOTH delivered file facts and
# agent-action path detection.
_SRC_EXT = (r"(?:py|pyi|go|ts|tsx|js|jsx|mjs|cjs|java|rb|rs|c|cc|cpp|cxx|h|hpp|"
            r"kt|kts|scala|php|swift|m|mm|cs)")
_FILE_RE = re.compile(r"(?<![\w])((?:[\w.\-]+/)*[\w.\-]+\.%s)(?![\w])" % _SRC_EXT)

# --- delivered-fact patterns (block-aware; see extract_facts) ---
_DEF_ARROW = re.compile(r"\bdef\s+(\w+)\s*->\s*(?:[\w.\-/]+\.%s)" % _SRC_EXT)
_DOUBLE_COLON = re.compile(r"((?:[\w.\-]+/)*[\w.\-]+\.%s)\s*::\s*(\w+)" % _SRC_EXT)
_CALLS_FUNC = re.compile(r"->\s*calls\s+func\s+(\w+)")
_DEF_PAREN = re.compile(r"\b(?:def|func|function)\s+(\w+)\s*\(")
_RESOLVED_CALLER = re.compile(r"resolved caller:\s*(\w+)\s*\(")
_RESOLVED_CALL = re.compile(r"resolved call:\s*->\s*(\w+)\s*\(")
_CALLERS = re.compile(r"[Cc]allers?:\s*(\w+)\s*\(")
_CALLED_BY = re.compile(r"called by:\s*(\w+)")
_NUM_ITEM = re.compile(r"^\s*\d+\.\s+(.*\S)")            # "  1. file — sym, sym"
_OBLIG_ITEM = re.compile(r"^\s*[-*]\s*(?:\[[^\]]*\]\s*)?(.*\S)")
_GT_TAG = re.compile(r"<(/?)(gt-[\w\-]+)")

# --- agent-action classifiers ---
_GREP_BIN = re.compile(r"\b(?:grep|egrep|fgrep|rg|ag|ack)\b")
_GIT_GREP = re.compile(r"\bgit\s+grep\b")
_FIND_BIN = re.compile(r"\b(?:find|fd)\b")
_FIND_NAME_ARG = re.compile(r"-(?:name|iname|path|regex)\s+['\"]?([^'\"\s]+)")
_VIEW_VERB = re.compile(r"\b(?:cat|head|tail|less|more|nl|view|open|bat)\b")
_SED_VIEW = re.compile(r"\bsed\s+-n\b")
_EDIT_SIG = re.compile(
    r"(?:str_replace|apply_patch|insert_line|new_str|\bpatch\b|\btee\b|"
    r"sed\s+-i|>>?\s*[\w./\-]+\.%s|python[0-9.]*\s+-c)" % _SRC_EXT)
_REDIR_TARGET = re.compile(r">>?\s*([\w./\-]+\.%s)" % _SRC_EXT)
_TEST_RUN = re.compile(
    r"\b(?:pytest|py\.test|unittest|nosetests|tox|go\s+test|cargo\s+test|"
    r"npx\s+jest|jest|vitest|mocha|mvn\s+(?:test|verify)|gradle\s+test|"
    r"rspec|ruby\s+-Itest|npm\s+(?:test|run\s+test)|yarn\s+test|ctest|"
    r"phpunit|dotnet\s+test)\b")
# A grep pattern lives after the grep binary + flags: first quoted string, else the
# first non-flag token. We also grab ALL quoted strings on the line for robustness.
_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_INFRA_PROBE = re.compile(
    r"gt_root\.txt|/opt/gt|\bgo\.mod\b|package\.json|pyproject|Cargo\.toml|"
    r"tsconfig|site-packages|node_modules")
_SCRATCH = re.compile(r"^(?:tmp|var/tmp|dev)/")
# F4: harness commands are prefixed with a working-dir hop (the parity harness emits
# 'cd $(cat /tmp/gt_root.txt) && <real command>'). The prefix is never the semantic
# action, but its literal text (gt_root.txt) trips _INFRA_PROBE and swallows the real
# grep => searches misclassified 'other'/'view', re-acquisition undercounted, and the
# L4 no-further-search guard goes vacuous. Strip leading 'cd <arg> &&/;' hops (repeat)
# BEFORE classification; ev['raw'] keeps the ORIGINAL command for evidence honesty.
_CD_PREFIX = re.compile(
    r"^\s*cd\s+(?:\$\([^)]*\)|\"[^\"]*\"|'[^']*'|[^&;|]+?)\s*(?:&&|;)\s*")


def _strip_cd_prefix(cmd: str) -> str:
    c = cmd or ""
    prev = None
    while prev != c:
        prev = c
        c = _CD_PREFIX.sub("", c)
    return c


# F2 (honesty rename): <gt-verify> payloads carry verify-NUDGE mentions of EDITED
# PRODUCTION FILES (test identifiers are leak-scrubbed by design; 0/98 test-like tokens
# measured on the 124-run corpus). The class measures "post-nudge: the agent ran a test
# naming the delivered file, and it went green" — NOT executed-RED covering-test
# consumption (that requires the seam's own receipt_state, W2, when live).
VERIFY_NUDGE_CLS = "verify_nudge_file"
FACT_CLASSES = ("localization-file", "symbol-definition", "caller-fact",
                VERIFY_NUDGE_CLS, "obligation")
# Pre-existing output keys (delivered_fact_count / delivered_fact_count_total) keep the
# LEGACY class name for byte-stability (baseline-diff law); receipt output uses the
# honest name. Documented in every report via fact_class_legacy_aliases.
_LEGACY_CLASS_ALIAS = {VERIFY_NUDGE_CLS: "covering-test"}

# B-7: grader MEASUREMENT class -> the paired counterfactual metric that grades its causal
# contribution (mirrors src/groundtruth/runtime/fact_registry.py FactRegistration.causal_eval;
# this instrument keeps its OWN measurement-class names, so the map is the honest cross-walk,
# NOT an import dependency — the grader stays decoupled + runnable standalone). Referenced
# ONLY inside compute_causal (the paired path); single-arm grading never touches it.
_CAUSAL_EVAL_METHOD = {
    "localization-file": "paired_steps_to_first_correct_edit",
    "symbol-definition": "paired_wrong_branch_reads_delta",
    "caller-fact": "paired_contract_break_rate",
    VERIFY_NUDGE_CLS: "paired_repair_targeting_delta",
    "obligation": "paired_plan_coverage_delta",
}


def _legacy_class_map(per_class: dict) -> dict:
    """Map internal class names to legacy names for the pre-existing output keys."""
    return {_LEGACY_CLASS_ALIAS.get(c, c): v for c, v in per_class.items()}


def _np(p: str) -> str:
    """Normalize a path: backslashes -> /, strip a leading ./ (mirrors measure_brief._np)."""
    return (p or "").replace("\\", "/").lstrip("./")


def _basename(p: str) -> str:
    return _np(p).rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# RECEIPT-LADDER detectors (word-boundary + path-aware — the FP guards).
# ---------------------------------------------------------------------------
_IDENT_RE = re.compile(r"[A-Za-z_]\w+")
# test-run observation verdict (RED->green): a pass marker AND no failure marker.
_TEST_GREEN = re.compile(r"\bpassed\b|\bok\b|<returncode>0</returncode>|"
                         r"\ball tests passed\b", re.I)
_TEST_FAIL = re.compile(r"\bfailed\b|\berrors?\b|\bfailures?\b|\btraceback\b|"
                        r"<returncode>[1-9]", re.I)


def _identifier_tokens(text: str) -> set:
    """Whole-word identifier tokens (len>=_MIN_SYMBOL_LEN, not a bare keyword).
    Word-boundary tokenization is the FP guard: 'running' -> {'running'} (never
    yields the substring 'run'); 'target_helper' -> {'target_helper'} (never 'get')."""
    return {t for t in _IDENT_RE.findall(text or "")
            if len(t) >= _MIN_SYMBOL_LEN and t.lower() not in _KEYWORD_STOP}


def _names_symbol(token: str, text: str, substring: bool) -> bool:
    """Does an agent message/command NAME a delivered symbol? Word-boundary by
    default; ``substring`` is the M1 mutation handle (reverts to inflating `in`)."""
    if not token or len(token) < _MIN_SYMBOL_LEN or token.lower() in _KEYWORD_STOP:
        return False
    if substring:
        return token in (text or "")
    return token in _identifier_tokens(text)


def _names_file(fpath: str, text: str, ambiguous_basenames: set, substring: bool) -> bool:
    """Does a message NAME a delivered file? Full relpath match, else the file stem
    as a whole token — but ONLY when the basename is unambiguous among delivered files."""
    p = _np(fpath)
    if not p:
        return False
    if p in _np(text or ""):
        return True
    base = _basename(p)
    stem = base.rsplit(".", 1)[0]
    if not base or base in ambiguous_basenames:
        return False
    if substring:
        return base in (text or "")
    return len(stem) >= _MIN_SYMBOL_LEN and stem in _identifier_tokens(text)


def _file_targets(action_file: str, fpath: str, ambiguous_basenames: set) -> bool:
    """Path-aware file-equality: full relpath (either direction), else basename ONLY
    when unambiguous. Collision-safe: a/util.py never matches b/util.py."""
    ef = _np(action_file or "")
    ft = _np(fpath or "")
    if not ef or not ft:
        return False
    if ef == ft or ef.endswith("/" + ft) or ft.endswith("/" + ef):
        return True
    be = _basename(ef)
    return bool(be and be == _basename(ft) and be not in ambiguous_basenames)


def _command_tokens(ev: dict) -> set:
    """Identifier tokens an action command names (needle tokens + raw-command tokens)."""
    return set(ev.get("needle_tokens") or set()) | _identifier_tokens(ev.get("raw") or "")


def _assoc_file(fact: dict) -> str | None:
    """The file a fact lives in: the file token itself, else the first source path in
    the delivered raw line (e.g. 'def foo -> src/mod.py:10' -> src/mod.py)."""
    if fact.get("kind") == "file":
        return fact.get("token")
    m = _FILE_RE.search(fact.get("raw") or "")
    return _np(m.group(1)) if m else None


def _test_obs_is_green(obs: str) -> bool:
    """Deterministic RED->green verdict from a test-run observation: a pass marker
    present AND no failure marker. Correct-or-quiet (absent obs => not green)."""
    o = obs or ""
    if not o or _TEST_FAIL.search(o):
        return False
    return bool(_TEST_GREEN.search(o))


def _test_cmd_names_file(fpath: str, raw_cmd: str) -> bool:
    """F1 STRICT file-identity: does the test COMMAND name the delivered file?
    Full relpath substring, else the basename with a word boundary on the LEFT
    (so 'test_bar.py::TestX' matches but 'contest_bar.py' does not)."""
    p = _np(fpath)
    r = _np(raw_cmd or "")
    if p and p in r:
        return True
    base = _basename(p)
    if not base:
        return False
    return bool(re.search(r"(?<![\w])" + re.escape(base), raw_cmd or ""))


_CODE_SHAPED = re.compile(r"^__\w+__$|_|[a-z][A-Z]")


def _code_shaped_tokens(tokens: set) -> set:
    """F5 heuristic middle: tokens that LOOK like code (dunder / snake_case underscore
    / camelCase). Explicitly heuristic — reported as a bounds column, never the
    decision column."""
    return {t for t in tokens if _CODE_SHAPED.search(t)}


# ===========================================================================
# DELIVERED-FACT EXTRACTION (block-aware state machine)
# ===========================================================================
def extract_facts(text: str, delivered_turn: int) -> list[dict]:
    """Extract delivered FACT UNITS from a GT payload (brief or in-stream block).

    Block-aware: tracks the current <gt-*> tag so a line is classified by the block
    it lives in (obligations vs verify vs localization) AND by its pattern. Returns
    a list of fact dicts; dedup happens in the caller. ``token`` is the SEARCHABLE
    identifier (a symbol name, or a file path); obligation facts have kind='prose'
    and are counted but never matched as re-acquisitions.
    """
    facts: list[dict] = []
    if not text:
        return facts
    cur_block: str | None = None

    def add(cls: str, kind: str, token, raw: str):
        facts.append({"cls": cls, "kind": kind,
                      "token": token, "delivered_turn": int(delivered_turn),
                      "raw": raw.strip()[:200]})

    for raw in text.splitlines():
        line = raw.rstrip()
        # tag transitions (update BEFORE classifying so opening-tag-line content counts)
        for m in _GT_TAG.finditer(line):
            cur_block = None if m.group(1) else m.group(2)

        stripped = line.strip()
        if not stripped or stripped.startswith("<gt-") or stripped.startswith("</gt-"):
            # pure tag / blank line — but a tag line may carry trailing content; the
            # DOUBLE_COLON / file patterns below still run on the raw line, so only
            # skip when there is genuinely no payload after the tag.
            after = _GT_TAG.sub("", line).strip()
            if not after:
                continue
            line_for_scan = after
        else:
            line_for_scan = line

        # 1. OBLIGATIONS block -> prose facts (not searchable)
        if cur_block == "gt-obligations":
            mo = _OBLIG_ITEM.match(line)
            if mo:
                add("obligation", "prose", None, mo.group(1))
            continue

        # 2. VERIFY block -> verify_nudge_file facts (F2: these are nudge mentions of
        #    EDITED PRODUCTION FILES — test ids are leak-scrubbed — not covering tests).
        if cur_block == "gt-verify":
            for fm in _FILE_RE.finditer(line_for_scan):
                add(VERIFY_NUDGE_CLS, "file", _np(fm.group(1)), line)
            continue

        matched_symbol_line = False

        # 3. caller facts (highest-specificity symbol facts)
        for pat in (_RESOLVED_CALLER, _RESOLVED_CALL, _CALLERS, _CALLED_BY):
            for cm in pat.finditer(line_for_scan):
                add("caller-fact", "symbol", cm.group(1), line)
                matched_symbol_line = True

        # 4. symbol-definition facts
        for dm in _DEF_ARROW.finditer(line_for_scan):
            add("symbol-definition", "symbol", dm.group(1), line)
            matched_symbol_line = True
        for dm in _DOUBLE_COLON.finditer(line_for_scan):
            add("localization-file", "file", _np(dm.group(1)), line)
            add("symbol-definition", "symbol", dm.group(2), line)
            matched_symbol_line = True
        for dm in _CALLS_FUNC.finditer(line_for_scan):
            add("symbol-definition", "symbol", dm.group(1), line)
            matched_symbol_line = True

        # 5. localization candidate line inside <gt-localization>: "N. file — sym, sym"
        if cur_block == "gt-localization":
            ni = _NUM_ITEM.match(line)
            if ni:
                body = ni.group(1)
                fm = _FILE_RE.search(body)
                if fm:
                    add("localization-file", "file", _np(fm.group(1)), line)
                # trailing "— sym, sym, sym" symbol list (only the em/hyphen tail)
                tail = re.split(r"\s[—-]\s", body, maxsplit=1)
                if len(tail) == 2:
                    for tok in re.split(r"[,\s]+", tail[1]):
                        tok = tok.strip()
                        if re.fullmatch(r"[A-Za-z_]\w+", tok):
                            add("symbol-definition", "symbol", tok, line)
                matched_symbol_line = True

        # 6. generic file paths -> localization-file (default), unless the line was a
        #    pure symbol carrier already handled. Files can co-occur with symbols
        #    (e.g. "def foo -> src/mod.py:10"): we WANT both the symbol and the file.
        for fm in _FILE_RE.finditer(line_for_scan):
            add("localization-file", "file", _np(fm.group(1)), line)

        _ = matched_symbol_line  # (kept for readability of the priority ladder)

    return facts


def _dedup_facts(facts: list[dict]) -> list[dict]:
    """Distinct facts by (cls, kind, token) for counting; keep earliest delivered_turn.
    Prose (obligation) facts dedup by their raw text (token is None)."""
    best: dict[tuple, dict] = {}
    for f in facts:
        key = (f["cls"], f["kind"], f["token"] if f["token"] is not None else f["raw"])
        cur = best.get(key)
        if cur is None or f["delivered_turn"] < cur["delivered_turn"]:
            best[key] = f
    return [best[k] for k in sorted(best, key=lambda k: (str(k[0]), str(k[2])))]


def _searchable_facts(facts: list[dict]) -> list[dict]:
    """Distinct symbol/file facts eligible to be re-acquired (dedup by kind+token).
    Symbols must clear the FP guard (len>=3, not a keyword). Files dedup by path."""
    seen: dict[tuple, dict] = {}
    for f in facts:
        if f["kind"] == "symbol":
            tok = f["token"] or ""
            if len(tok) < _MIN_SYMBOL_LEN or tok.lower() in _KEYWORD_STOP:
                continue
            key = ("symbol", tok)
        elif f["kind"] == "file":
            key = ("file", f["token"])
        else:
            continue
        cur = seen.get(key)
        if cur is None or f["delivered_turn"] < cur["delivered_turn"]:
            seen[key] = f
    return [seen[k] for k in sorted(seen, key=lambda k: (k[0], str(k[1])))]


# ===========================================================================
# TRAJECTORY LOADING (shape-aware) — mirrors gt_deep_metrics / measure_brief
# ===========================================================================
def _canonical_trajectory(run_dir: str) -> tuple[str | None, str]:
    """Return (path, kind) for the run's trajectory. kind in {'output_jsonl',
    'miniswe_trajectory','none'}. Prefers OpenHands output.jsonl, then the mini-swe
    trajectory (incl. pier truncated jobs/**/agent dirs and retry attempts)."""
    oj = sorted(_glob.glob(os.path.join(run_dir, "**", "output.jsonl"), recursive=True))
    if oj:
        return oj[0], "output_jsonl"
    for name in ("mini-swe-agent.trajectory.json", "*.trajectory.json"):
        hits = sorted(_glob.glob(os.path.join(run_dir, "**", "agent", name), recursive=True))
        if not hits:
            hits = sorted(_glob.glob(os.path.join(run_dir, "**", name), recursive=True))
        if hits:
            return hits[0], "miniswe_trajectory"
    attempts = sorted(_glob.glob(
        os.path.join(run_dir, "**", "*.trajectory.attempt*.json"), recursive=True))
    if attempts:
        return attempts[-1], "miniswe_trajectory"
    return None, "none"


def _gt_regions(text: str) -> str:
    """The concatenated <gt-*>...</gt-*> regions of a text (so issue prose in the
    brief is NOT mined as GT facts — only GT-authored blocks are)."""
    if not text:
        return ""
    return "\n".join(m.group(0) for m in re.finditer(
        r"<gt-[\w\-]+[^>]*>.*?</gt-[\w\-]+>", text, re.S))


def _is_gt_delivery(obs: str) -> bool:
    return bool(obs) and ("<gt-" in obs or obs.lstrip().startswith("GT:")
                          or obs.lstrip().startswith("[GT]"))


def _classify_command(cmd: str) -> dict:
    """Classify one bash command into an event. Priority edit>test>search>view>other.
    Classification runs on the cd-prefix-STRIPPED text (F4); ``raw`` keeps the
    original command bytes."""
    c = _strip_cd_prefix(cmd or "")
    ev = {"type": "other", "file": None, "needle": "", "needle_tokens": set(),
          "raw": (cmd or "").strip()[:300]}
    is_edit = bool(_EDIT_SIG.search(c))
    is_test = bool(_TEST_RUN.search(c))
    is_search = bool((_GREP_BIN.search(c) or _GIT_GREP.search(c)
                      or (_FIND_BIN.search(c) and _FIND_NAME_ARG.search(c)))
                     and not _INFRA_PROBE.search(c))
    if is_edit and not is_test:
        ev["type"] = "edit"
        m = _REDIR_TARGET.search(c)
        if m and not _SCRATCH.search(_np(m.group(1))):
            ev["file"] = _np(m.group(1))
        else:
            paths = [_np(p) for p in _FILE_RE.findall(c) if not _SCRATCH.search(_np(p))]
            ev["file"] = paths[0] if paths else None
        return ev
    if is_test:
        ev["type"] = "test"
        return ev
    if is_search:
        ev["type"] = "search"
        ev["needle"], ev["needle_tokens"] = _search_needle(c)
        return ev
    if (_VIEW_VERB.search(c) or _SED_VIEW.search(c)) and not is_edit:
        paths = [_np(p) for p in _FILE_RE.findall(c) if not _SCRATCH.search(_np(p))]
        if paths:
            ev["type"] = "view"
            ev["file"] = paths[0]
            return ev
    return ev


def _search_needle(cmd: str) -> tuple[str, set]:
    """Extract the search PATTERN (not the scope) from a grep/find command, and its
    word tokens (len>=3). For grep: the first quoted string, else the first non-flag
    token after the grep binary. For find: the -name/-path/-regex argument."""
    needle_parts: list[str] = []
    fn = _FIND_NAME_ARG.search(cmd)
    if fn:
        needle_parts.append(fn.group(1))
    gm = _GREP_BIN.search(cmd) or _GIT_GREP.search(cmd)
    if gm:
        rest = cmd[gm.end():]
        q = _QUOTED.search(rest)
        if q:
            needle_parts.append(q.group(1))
        else:
            for tok in rest.split():
                if tok.startswith("-"):
                    continue
                needle_parts.append(tok)
                break
    needle = " ".join(needle_parts)
    tokens = {t for t in re.findall(r"[A-Za-z_]\w+", needle) if len(t) >= _MIN_SYMBOL_LEN}
    return needle, tokens


def _iter_miniswe(traj: dict) -> list[dict]:
    """Ordered timeline from a mini-swe-agent trajectory (messages[]). Emits action
    events (with a turn ordinal) and delivery markers. Handles the chat shape
    (role=assistant, extra.actions[].command) AND the Responses-API shape
    (role/type None with an output[] list) — mirrors gt_deep_metrics._from_miniswe."""
    timeline: list[dict] = []
    turn = 0
    for m in traj.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        mtype = m.get("type")
        content = m.get("content")
        if isinstance(content, list):
            content = json.dumps(content)
        content = content or ""
        extra = m.get("extra") if isinstance(m.get("extra"), dict) else {}

        is_responses_turn = role is None and mtype is None and isinstance(m.get("output"), list)
        is_chat_turn = role == "assistant"

        if is_chat_turn or is_responses_turn:
            cmds: list[str] = []
            for a in (extra.get("actions") or []):
                c = a.get("command") if isinstance(a, dict) else None
                if isinstance(c, str):
                    cmds.append(c)
            if is_responses_turn:
                for it in (m.get("output") or []):
                    if isinstance(it, dict) and it.get("type") == "function_call":
                        arg = it.get("arguments") or ""
                        try:
                            cmds.append(str((json.loads(arg) or {}).get("command", arg)))
                        except (ValueError, TypeError):
                            cmds.append(str(arg))
            # agent MESSAGE text (the reasoning) — the REFERENCED-receipt surface.
            msg_text = content
            if is_responses_turn:
                parts = []
                for it in (m.get("output") or []):
                    if isinstance(it, dict) and it.get("type") in (
                            "message", "reasoning", "output_text"):
                        ct = it.get("content") or it.get("text") or ""
                        parts.append(ct if isinstance(ct, str) else json.dumps(ct))
                if parts:
                    msg_text = "\n".join(parts)
            if isinstance(msg_text, str) and msg_text.strip():
                timeline.append({"type": "agent_message", "turn": turn, "text": msg_text})
            for c in cmds:
                turn += 1
                ev = _classify_command(c)
                ev["turn"] = turn
                timeline.append(ev)
            continue

        # OBSERVATION (tool / exit / function_call_output) — GT in-stream delivery.
        if mtype == "function_call_output" or role in ("tool", "exit"):
            obs = content or m.get("output") or m.get("result") or ""
            if isinstance(obs, list):
                obs = json.dumps(obs)
            if _is_gt_delivery(obs or ""):
                timeline.append({"type": "gt_delivery", "turn": turn, "text": obs})
            # every observation is also emitted so load_run can attach it to the
            # preceding action (RESOLVED_STATE test-green verdict reads obs_text).
            timeline.append({"type": "observation", "turn": turn, "text": obs or ""})
            continue

        # first user/system message may carry the GT brief (delivered_turn 0)
        if role in ("user", "system"):
            regions = _gt_regions(content)
            if regions:
                timeline.append({"type": "gt_delivery", "turn": 0, "text": regions})
    return timeline


def _iter_openhands(doc: dict) -> list[dict]:
    """Ordered timeline from an OpenHands output.jsonl doc (.history[])."""
    timeline: list[dict] = []
    turn = 0
    instr = doc.get("instruction") or ""
    regions = _gt_regions(instr)
    if regions:
        timeline.append({"type": "gt_delivery", "turn": 0, "text": regions})
    for e in doc.get("history", []) or []:
        if not isinstance(e, dict):
            continue
        action = e.get("action")
        source = e.get("source")
        # OBSERVATION event: no action (or explicit observation key)
        if e.get("observation") is not None or (action is None and e.get("content")):
            obs = e.get("content") or ""
            if _is_gt_delivery(obs):
                timeline.append({"type": "gt_delivery", "turn": turn, "text": obs})
            timeline.append({"type": "observation", "turn": turn, "text": obs})
            continue
        if source != "agent" or not action:
            continue
        args = e.get("args") or {}
        if action in ("message", "think"):
            txt = args.get("content") or args.get("thought") or e.get("message") or ""
            if isinstance(txt, str) and txt.strip():
                timeline.append({"type": "agent_message", "turn": turn, "text": txt})
            continue
        if action == "read":
            turn += 1
            timeline.append({"type": "view", "file": _np(args.get("path") or ""),
                             "needle": "", "needle_tokens": set(),
                             "raw": ("read " + str(args.get("path") or ""))[:300],
                             "turn": turn})
        elif action in ("edit", "write", "str_replace_editor"):
            turn += 1
            timeline.append({"type": "edit", "file": _np(args.get("path") or ""),
                             "needle": "", "needle_tokens": set(),
                             "raw": (action + " " + str(args.get("path") or ""))[:300],
                             "turn": turn})
        elif action in ("run", "run_ipython"):
            turn += 1
            ev = _classify_command(str(args.get("command") or ""))
            ev["turn"] = turn
            timeline.append(ev)
        # message/system/recall/think/finish/error -> not a tool action, no turn
    return timeline


def load_run(run_dir: str) -> dict:
    """Load a run into normalized (delivered_facts, events, meta). Never raises."""
    out = {
        "run_dir": run_dir,
        "trajectory_source": "none",
        "trajectory_path": "",
        "delivered_facts": [],
        "events": [],
        "agent_messages": [],
    }
    if not run_dir or not os.path.isdir(run_dir):
        return out

    timeline: list[dict] = []

    # (a) delivered_instruction.txt at the run root = the GT brief (delivered_turn 0).
    di = os.path.join(run_dir, "delivered_instruction.txt")
    if os.path.isfile(di):
        try:
            with open(di, encoding="utf-8", errors="replace") as fh:
                timeline.append({"type": "gt_delivery", "turn": 0, "text": fh.read()})
        except OSError:
            pass

    # (b) the trajectory (OH or mini) — actions + in-stream GT deliveries + brief.
    tpath, kind = _canonical_trajectory(run_dir)
    out["trajectory_source"] = "miniswe_trajectory" if kind == "miniswe_trajectory" else (
        "output_jsonl" if kind == "output_jsonl" else "none")
    out["trajectory_path"] = tpath or ""
    if tpath and os.path.isfile(tpath):
        try:
            if kind == "output_jsonl":
                with open(tpath, encoding="utf-8", errors="replace") as fh:
                    doc = json.loads(fh.readline())
                timeline += _iter_openhands(doc)
            else:
                with open(tpath, encoding="utf-8", errors="replace") as fh:
                    doc = json.load(fh)
                timeline += _iter_miniswe(doc)
        except (OSError, ValueError):
            pass

    # split timeline into facts (from every gt_delivery), action events, and agent
    # messages. A global chronological ``order`` (enumeration index) is stamped so
    # receipt precedence is unambiguous; the existing per-event ``turn`` is untouched
    # (existing metrics stay byte-stable). Observations attach to the preceding action.
    facts: list[dict] = []
    events: list[dict] = []
    messages: list[dict] = []
    last_action: dict | None = None
    for order, item in enumerate(timeline):
        typ = item.get("type")
        if typ == "gt_delivery":
            new = extract_facts(item.get("text") or "", item.get("turn", 0))
            for f in new:
                f["delivered_order"] = order
            facts += new
        elif typ == "agent_message":
            item["order"] = order
            messages.append(item)
        elif typ == "observation":
            if last_action is not None and last_action.get("obs_text") is None:
                last_action["obs_text"] = item.get("text") or ""
        else:
            item["order"] = order
            item.setdefault("obs_text", None)
            events.append(item)
            last_action = item
    events.sort(key=lambda e: e.get("turn", 0))
    out["delivered_facts"] = facts
    out["events"] = events
    out["agent_messages"] = messages
    return out


# ===========================================================================
# METRICS
# ===========================================================================
def _fact_matches_search(fact: dict, ev: dict) -> bool:
    """Does agent SEARCH event ev re-acquire delivered fact? (word-boundary / basename)"""
    if fact["kind"] == "symbol":
        tok = fact["token"] or ""
        if len(tok) < _MIN_SYMBOL_LEN or tok.lower() in _KEYWORD_STOP:
            return False
        return tok in (ev.get("needle_tokens") or set())
    if fact["kind"] == "file":
        base = _basename(fact["token"] or "")
        stem = base.rsplit(".", 1)[0]
        needle = ev.get("needle") or ""
        toks = ev.get("needle_tokens") or set()
        return bool(base and (base in needle or (len(stem) >= _MIN_SYMBOL_LEN and stem in toks)))
    return False


def compute_re_acquisition(delivered_facts: list[dict], events: list[dict],
                           enforce_precedence: bool = True) -> dict:
    """RQ2 core. A re-acquisition = a SEARCH event whose needle matches a delivered
    searchable fact that (under precedence) PRECEDED it. One event per DISTINCT
    re-acquired fact (first offending search). ``enforce_precedence=False`` is the
    MUTATION handle — it drops the delivered_turn<search_turn guard so the test can
    prove that guard is load-bearing."""
    searchable = _searchable_facts(delivered_facts)
    searches = [e for e in events if e.get("type") == "search"]
    reacq: list[dict] = []
    for fact in searchable:
        hit = None
        for ev in searches:
            if not _fact_matches_search(fact, ev):
                continue
            if enforce_precedence and ev.get("turn", 0) <= fact["delivered_turn"]:
                continue
            hit = ev
            break
        if hit is not None:
            reacq.append({
                "fact_token": fact["token"],
                "fact_class": fact["cls"],
                "fact_kind": fact["kind"],
                "delivered_turn": fact["delivered_turn"],
                "search_turn": hit.get("turn", 0),
                "search_raw": hit.get("raw", ""),
            })
    reacq.sort(key=lambda r: (r["delivered_turn"], r["search_turn"], str(r["fact_token"])))
    n_searchable = len(searchable)
    n_reacq = len(reacq)
    rate = round(1.0 - (n_reacq / n_searchable), 8) if n_searchable else None
    return {
        "searchable_delivered_fact_count": n_searchable,
        "re_acquisition_events": reacq,
        "re_acquisition_event_count": n_reacq,
        "re_acquired_fact_count": n_reacq,
        "non_re_acquisition_rate": rate,
    }


def _distinct_for_receipts(facts: list[dict]) -> list[dict]:
    """Distinct facts by (cls,kind,token|raw) keeping the EARLIEST delivered_order.
    Mirrors _dedup_facts's key but preserves delivered_order + raw for the detectors."""
    best: dict[tuple, dict] = {}
    for f in facts:
        key = (f["cls"], f["kind"], f["token"] if f["token"] is not None else f.get("raw"))
        o = f.get("delivered_order", f.get("delivered_turn", 0) or 0)
        cur = best.get(key)
        if cur is None or o < cur.get("delivered_order", 1 << 30):
            g = dict(f)
            g["delivered_order"] = o
            best[key] = g
    return [best[k] for k in sorted(best, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))]


def _dedup_localization_groups(loc_facts: list[dict]) -> dict:
    """F3: collapse localization-file path VARIANTS (context.py / fsm/context.py /
    aiogram/fsm/context.py) into suffix-identity groups (union-find: equal, or one
    endswith '/'+other). Group receipt = MAX level over variants. Returns the DEDUPED
    distribution block — the denominator ADMIT/CUT decisions must cite."""
    toks = sorted({p["token"] for p in loc_facts if p.get("token")})
    parent = {t: t for t in toks}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(toks):
        for b in toks[i + 1:]:
            if a.endswith("/" + b) or b.endswith("/" + a):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
    groups: dict[str, list[str]] = {}
    for t in toks:
        groups.setdefault(find(t), []).append(t)
    best = {p["token"]: p["highest_level_num"] for p in loc_facts if p.get("token")}
    n = len(groups)
    dist = {name: 0 for name in _RECEIPT_ORDER}
    for members in groups.values():
        top = max(best[m] for m in members)
        dist[_RECEIPT_ORDER[top - 1]] += 1
    at_least: dict[str, int] = {}
    acc = 0
    for name in reversed(_RECEIPT_ORDER):
        acc += dist[name]
        at_least[name] = acc
    return {
        "group_count": n,
        "highest_level_distribution": dist,
        "at_least": at_least,
        "rate": {name: (round(at_least[name] / n, 8) if n else None)
                 for name in _RECEIPT_ORDER},
    }


def compute_receipts(delivered_facts: list[dict], events: list[dict],
                     agent_messages: list[dict],
                     use_substring_reference: bool = False,
                     enforce_no_further_search: bool = True,
                     arms: str = ARMS_SINGLE) -> dict:
    """Per-fact consumption receipt ladder (levels 1-4) over ONE run's trajectory.

    For each DISTINCT delivered fact, decide the highest receipt level it reached:
      * 1 DELIVERED       — the fact was delivered (floor for every delivered fact).
      * 2 REFERENCED      — a LATER agent message NAMES the delivered entity
                            (word-boundary token / full relpath; NOT substring).
      * 3 ACTED           — a LATER action TARGETS the entity (command names the
                            symbol, or the action file path-matches the fact's file).
      * 4 RESOLVED_STATE  — the predicted observable changed: for a def-site/loc file,
                            an edit LANDS in that file AND no further search re-acquires
                            it; for a verify_nudge_file fact, a later test run whose
                            command NAMES the delivered file goes GREEN (STRICT, F1 —
                            the target-blind any-green variant is the WEAK column).
    Level 5 CAUSAL is paired-only (documented in RECEIPT_LADDER_NOTE, never computed).

    Two MUTATION handles (kept load-bearing by TTD):
      * ``use_substring_reference``  — M1: revert REFERENCED to inflating substring `in`.
      * ``enforce_no_further_search``— M2: drop RESOLVED_STATE's no-re-acquisition guard.
    """
    distinct = _distinct_for_receipts(delivered_facts)
    file_bn = Counter(_basename(f["token"]) for f in distinct
                      if f["kind"] == "file" and f["token"])
    ambiguous = {b for b, c in file_bn.items() if c > 1}
    # DISTINCTIVE code-entity tokens: the run's OWN delivered symbols + file stems.
    # An obligation (prose) is REFERENCED only when it names one of THESE and it recurs
    # in a later message — anchoring "distinctive" to real code, not generic English
    # words (which would inflate prose receipts; the W2 inflation lesson).
    entity_tokens: set = set()
    for f in distinct:
        if f["kind"] == "symbol" and f["token"]:
            t = f["token"]
            if len(t) >= _MIN_SYMBOL_LEN and t.lower() not in _KEYWORD_STOP:
                entity_tokens.add(t)
        elif f["kind"] == "file" and f["token"]:
            stem = _basename(f["token"]).rsplit(".", 1)[0]
            if len(stem) >= _MIN_SYMBOL_LEN and stem.lower() not in _KEYWORD_STOP:
                entity_tokens.add(stem)
    msgs = sorted(agent_messages or [], key=lambda m: m.get("order", 0))
    acts = sorted((e for e in events if e.get("type") != "observation"),
                  key=lambda e: e.get("order", 0))
    searches = [e for e in acts if e.get("type") == "search"]

    per_fact: list[dict] = []
    for f in distinct:
        do = f.get("delivered_order", 0)
        cls, kind, tok = f["cls"], f["kind"], f["token"]
        af = _assoc_file(f)
        lv = {RECEIPT_DELIVERED: True, RECEIPT_REFERENCED: False,
              RECEIPT_ACTED: False, RECEIPT_RESOLVED_STATE: False}
        # F5 bounds (prose only) / F1 weak column (verify class only); None elsewhere.
        ref_code_shaped = ref_loose = None
        resolved_any_green = None

        # ---- L2 REFERENCED: a later agent message names the entity ----
        if kind == "prose":
            # obligation bounds: lower = a DISTINCTIVE delivered code entity recurs
            # (the decision column); mid = code-shaped token recurs (HEURISTIC);
            # ceiling = any shared identifier token recurs (LOOSE upper bound).
            obl_all = _identifier_tokens(f.get("raw") or "")
            obl_anchor = obl_all & entity_tokens
            obl_code = _code_shaped_tokens(obl_all)
            ref_code_shaped = ref_loose = False
            for m in msgs:
                if m.get("order", 0) <= do:
                    continue
                mtoks = _identifier_tokens(m.get("text") or "")
                if obl_anchor & mtoks:
                    lv[RECEIPT_REFERENCED] = True
                if obl_code & mtoks:
                    ref_code_shaped = True
                if obl_all & mtoks:
                    ref_loose = True
                if lv[RECEIPT_REFERENCED] and ref_code_shaped and ref_loose:
                    break
        else:
            for m in msgs:
                if m.get("order", 0) <= do:
                    continue
                t = m.get("text") or ""
                if kind == "symbol" and _names_symbol(tok, t, use_substring_reference):
                    lv[RECEIPT_REFERENCED] = True
                    break
                if kind == "file" and _names_file(tok, t, ambiguous,
                                                  use_substring_reference):
                    lv[RECEIPT_REFERENCED] = True
                    break

        # ---- L3 ACTED: a later action targets the entity ----
        if kind == "symbol":
            for e in acts:
                if e.get("order", 0) <= do:
                    continue
                if tok in _command_tokens(e) \
                        or (af and _file_targets(e.get("file"), af, ambiguous)) \
                        or (af and _np(af) in _np(e.get("raw") or "")):
                    lv[RECEIPT_ACTED] = True
                    break
        elif kind == "file":
            for e in acts:
                if e.get("order", 0) <= do:
                    continue
                if _file_targets(e.get("file"), tok, ambiguous) \
                        or _np(tok) in _np(e.get("raw") or ""):
                    lv[RECEIPT_ACTED] = True
                    break
        # prose (obligation): acted is not deterministically attributable -> quiet.

        # ---- L4 RESOLVED_STATE: the predicted observable changed ----
        if cls == VERIFY_NUDGE_CLS and kind == "file":
            # F1: STRICT (decision column) = a later GREEN test run whose command
            # NAMES the delivered file. WEAK (any-green, target-blind) kept as a
            # separate reported column — never the decision.
            resolved_any_green = False
            for e in acts:
                if e.get("order", 0) > do and e.get("type") == "test" \
                        and _test_obs_is_green(e.get("obs_text")):
                    resolved_any_green = True
                    if _test_cmd_names_file(tok, e.get("raw")):
                        lv[RECEIPT_RESOLVED_STATE] = True
                        break
        elif af and kind in ("symbol", "file"):
            edit_lands = any(
                e.get("order", 0) > do and e.get("type") == "edit"
                and _file_targets(e.get("file"), af, ambiguous) for e in acts)
            if edit_lands:
                further = False
                if enforce_no_further_search:
                    further = any(
                        s.get("order", 0) > do and _fact_matches_search(f, s)
                        for s in searches)
                if not further:
                    lv[RECEIPT_RESOLVED_STATE] = True

        # B-7: this is a SINGLE-arm detector — cap at RESOLVED_STATE (4) so no fact can
        # ever carry CAUSAL (5) out of one trajectory. cap is a no-op today (the ladder
        # numbers 1-4) but makes the level-5 refusal explicit + mutation-testable.
        highest = cap_single_arm_level(
            max((_RECEIPT_NUM[n] for n, v in lv.items() if v), default=1))
        per_fact.append({
            "cls": cls, "kind": kind, "token": tok,
            "highest_level": _RECEIPT_ORDER[highest - 1],
            "highest_level_num": highest,
            "levels": {n: bool(lv[n]) for n in _RECEIPT_ORDER},
            # F1 weak column (verify class only; None elsewhere)
            "resolved_state_any_green": resolved_any_green,
            # F5 bounds (prose only; None elsewhere)
            "referenced_code_shaped_heuristic": ref_code_shaped,
            "referenced_loose_ceiling": ref_loose,
        })

    # ---- per-fact-class consumption table ----
    by_class: dict[str, dict] = {}
    for cls in FACT_CLASSES:
        fc = [p for p in per_fact if p["cls"] == cls]
        n = len(fc)
        dist = {name: 0 for name in _RECEIPT_ORDER}
        for p in fc:
            dist[p["highest_level"]] += 1
        at_least: dict[str, int] = {}
        acc = 0
        for name in reversed(_RECEIPT_ORDER):
            acc += dist[name]
            at_least[name] = acc
        rate = {name: (round(at_least[name] / n, 8) if n else None)
                for name in _RECEIPT_ORDER}
        blk = {
            "delivered_distinct": n,
            # B-7: arms label on EVERY per-class block so a single-arm RESOLVED_STATE rate
            # can never be misread as causal/effectiveness.
            "arms": arms,
            "highest_level_distribution": dict(dist),
            "at_least": {name: at_least[name] for name in _RECEIPT_ORDER},
            "rate": rate,
            # class-specific columns; None (=null) where not applicable.
            "resolved_state_weak_any_green": None,
            "rate_resolved_state_weak_any_green": None,
            "deduped": None,
            "referenced_bounds": None,
        }
        if cls == VERIFY_NUDGE_CLS:
            w = sum(1 for p in fc if p.get("resolved_state_any_green"))
            blk["resolved_state_weak_any_green"] = w
            blk["rate_resolved_state_weak_any_green"] = (round(w / n, 8) if n else None)
        if cls == "localization-file":
            blk["deduped"] = _dedup_localization_groups(fc)
        if cls == "obligation":
            lower = at_least[RECEIPT_REFERENCED]
            mid = sum(1 for p in fc if p.get("referenced_code_shaped_heuristic"))
            loose = sum(1 for p in fc if p.get("referenced_loose_ceiling"))
            blk["referenced_bounds"] = {
                "lower_entity_anchored": lower,
                "code_shaped_heuristic": mid,
                "loose_ceiling": loose,
                "rate_lower_entity_anchored": (round(lower / n, 8) if n else None),
                "rate_code_shaped_heuristic": (round(mid / n, 8) if n else None),
                "rate_loose_ceiling": (round(loose / n, 8) if n else None),
                "note": ("REFERENCED for prose is a LOWER BOUND (entity-anchored). "
                         "code_shaped is HEURISTIC; loose_ceiling is the untightened "
                         "any-token upper bound."),
            }
        by_class[cls] = blk
    admit_cut = {cls: by_class[cls]["highest_level_distribution"] for cls in FACT_CLASSES}
    return {
        "receipt_levels": list(_RECEIPT_ORDER),
        # B-7: the ARMS label + the explicit CAUSAL refusal. A single arm reports levels
        # 1-4 only; CAUSAL (receipt_causal_level, kept for documentation of the level NAME)
        # is NEVER a computed verdict here — receipt_causal is the honest verdict field and
        # is UNAVAILABLE(single-arm) for any single trajectory (gate item 12). Only
        # compute_causal() over a matched pair may produce a real causal verdict.
        "receipt_arms": arms,
        "receipt_causal": (CAUSAL_UNAVAILABLE_SINGLE_ARM if arms == ARMS_SINGLE else None),
        "receipt_causal_level": RECEIPT_CAUSAL,
        "receipt_ladder_note": RECEIPT_LADDER_NOTE,
        "receipt_by_class": by_class,
        "receipt_admit_cut": admit_cut,
        "receipt_per_fact": sorted(
            per_fact, key=lambda p: (p["cls"], p["kind"], str(p["token"]))),
    }


def _detect_arms(on_report: dict, off_report: dict) -> str:
    """B-7 structural single-arm detector behind the chokepoint. Returns :data:`ARMS_SINGLE`
    iff the two 'arms' are the SAME graded trajectory (identical object, or same run_dir +
    trajectory_path — one arm passed twice); :data:`ARMS_PAIRED` otherwise. Deterministic."""
    if on_report is off_report:
        return ARMS_SINGLE
    same_dir = on_report.get("run_dir") == off_report.get("run_dir")
    same_traj = on_report.get("trajectory_path") == off_report.get("trajectory_path")
    return ARMS_SINGLE if (same_dir and same_traj) else ARMS_PAIRED


def compute_causal(on_report: dict, off_report: dict) -> dict:
    """Level-5 CAUSAL — computable ONLY from a matched GT-on/GT-off pair (§0 gate item 12).

    ``on_report`` / ``off_report`` are single-arm :func:`grade_run` outputs for the SAME
    task (GT-on vs GT-off, same history prefix — the matched-pair contract the CALLER owns,
    since a task identity is not recoverable from a run dir alone). What this function
    ENFORCES is the single-arm refusal: two identical arms (or one report passed twice), or
    an ungraded arm, route through :func:`assert_paired_for_causal` and RAISE
    :class:`SingleArmCausalError` — a causal verdict can never come from one trajectory.

    Returns the paired counterfactual table (``arms='paired'``): run-level BEHAVIORAL deltas
    (off - on; positive = GT reduced the acquisition cost) + a per-fact-class RESOLVED_STATE
    -rate delta, keyed by fact_class, at 8-dp. Deterministic, LLM-free, no I/O.
    """
    for tag, rep in (("on", on_report), ("off", off_report)):
        if not isinstance(rep, dict) or rep.get("trajectory_source") in (None, "none"):
            raise SingleArmCausalError(
                f"CAUSAL needs two graded arms; the {tag!r} arm is ungraded/empty "
                "(no counterfactual can be grounded on a missing trajectory)")
    arms = _detect_arms(on_report, off_report)
    assert_paired_for_causal(arms)   # <-- THE CHOKEPOINT: single arm => SingleArmCausalError

    def _delta(key: str):
        """off - on, 8-dp; None unless both arms carry a real (non-bool) number."""
        a, b = off_report.get(key), on_report.get(key)
        if (isinstance(a, (int, float)) and not isinstance(a, bool)
                and isinstance(b, (int, float)) and not isinstance(b, bool)):
            return round(float(a) - float(b), 8)
        return None

    behavioral = {
        "turns_to_first_edit_delta": _delta("turns_to_first_edit"),
        "turns_to_first_gold_read_delta": _delta("turns_to_first_gold_read"),
        "turns_to_first_test_run_delta": _delta("turns_to_first_test_run"),
        "wrong_branch_reads_delta": _delta("wrong_branch_reads"),
        "action_count_delta": _delta("action_count"),
        "search_count_delta": _delta("search_count"),
        # non_re_acquisition is on - off (higher on = GT substituted more acquisition).
        "non_re_acquisition_rate_delta": (
            round(float(on_report["non_re_acquisition_rate"])
                  - float(off_report["non_re_acquisition_rate"]), 8)
            if isinstance(on_report.get("non_re_acquisition_rate"), (int, float))
            and isinstance(off_report.get("non_re_acquisition_rate"), (int, float))
            else None),
    }

    on_bc = on_report.get("receipt_by_class") or {}
    off_bc = off_report.get("receipt_by_class") or {}

    def _rs_rate(blk: dict):
        return (blk.get("rate") or {}).get(RECEIPT_RESOLVED_STATE)

    by_class: dict[str, dict] = {}
    for cls in FACT_CLASSES:
        on_r = _rs_rate(on_bc.get(cls) or {})
        off_r = _rs_rate(off_bc.get(cls) or {})
        delta = (round(float(on_r) - float(off_r), 8)
                 if isinstance(on_r, (int, float)) and isinstance(off_r, (int, float))
                 else None)
        by_class[cls] = {
            "arms": arms,
            "on_resolved_state_rate": on_r,
            "off_resolved_state_rate": off_r,
            "resolved_state_rate_delta": delta,
            "causal_eval_method": _CAUSAL_EVAL_METHOD.get(cls),
        }

    return {
        "receipt_arms": arms,
        "receipt_causal_level": RECEIPT_CAUSAL,
        "causal_available": True,
        "on_run_dir": _np(on_report.get("run_dir") or ""),
        "off_run_dir": _np(off_report.get("run_dir") or ""),
        "causal_behavioral_delta": behavioral,
        "causal_by_class": by_class,
        "causal_note": (
            "CAUSAL = paired GT-on vs GT-off counterfactual (verifyandobserve.md §0 gate "
            "item 12). Behavioral deltas are off - on (positive = GT reduced the "
            "acquisition cost); the per-class column is the RESOLVED_STATE-rate delta "
            "(on - off). Matched-task + same-history-prefix is the caller's contract; the "
            "single-arm refusal (identical/ungraded arm => SingleArmCausalError) is "
            "enforced structurally here."),
    }


def _gold_match(path: str, gold: set[str]) -> bool:
    p = _np(path)
    return bool(p) and (p in gold or any(p.endswith("/" + g) or g.endswith("/" + p)
                                         for g in gold))


def grade_run(run_dir: str, gold_files: list[str] | None = None) -> dict:
    """Full per-run report (8-dp, deterministic, no timestamps in payload)."""
    run = load_run(run_dir)
    facts = run["delivered_facts"]
    events = run["events"]
    gold = {_np(g) for g in (gold_files or [])}

    distinct = _dedup_facts(facts)
    per_class = {c: sum(1 for f in distinct if f["cls"] == c) for c in FACT_CLASSES}
    fact_file_set = {f["token"] for f in distinct if f["kind"] == "file" and f["token"]}

    reacq = compute_re_acquisition(facts, events)
    receipts = compute_receipts(facts, events, run.get("agent_messages") or [])

    # turn-to-X (agent-action ordinals)
    def _first_turn(pred):
        for e in events:
            if pred(e):
                return e.get("turn")
        return None

    edited_files = {e["file"] for e in events if e.get("type") == "edit" and e.get("file")}

    turns_to_first_edit = _first_turn(lambda e: e.get("type") == "edit")
    turns_to_first_test = _first_turn(lambda e: e.get("type") == "test")
    turns_to_first_gold_read = None
    if gold:
        turns_to_first_gold_read = _first_turn(
            lambda e: e.get("type") in ("view", "edit") and _gold_match(e.get("file") or "", gold))

    # wrong-branch reads: a VIEW of a file that is never later edited AND is not named
    # by any delivered file fact (localization/graph-map/covering-test).
    wrong_branch = 0
    wrong_branch_files = []
    for e in events:
        if e.get("type") != "view" or not e.get("file"):
            continue
        f = e["file"]
        if f in edited_files:
            continue
        if any(f == ff or f.endswith("/" + ff) or ff.endswith("/" + f) for ff in fact_file_set):
            continue
        wrong_branch += 1
        wrong_branch_files.append(f)

    counts = {
        "action_count": len(events),
        "search_count": sum(1 for e in events if e.get("type") == "search"),
        "view_count": sum(1 for e in events if e.get("type") == "view"),
        "edit_count": sum(1 for e in events if e.get("type") == "edit"),
        "test_count": sum(1 for e in events if e.get("type") == "test"),
    }

    report = {
        "run_dir": _np(run_dir),
        "trajectory_source": run["trajectory_source"],
        "trajectory_path": _np(run["trajectory_path"]),
        "gold_files": sorted(gold),
        # pre-existing key: LEGACY class names (byte-stability; see _LEGACY_CLASS_ALIAS)
        "delivered_fact_count": _legacy_class_map(per_class),
        "fact_class_legacy_aliases": dict(_LEGACY_CLASS_ALIAS),
        "delivered_fact_total_distinct": len(distinct),
        **reacq,
        "turns_to_first_edit": turns_to_first_edit,
        "turns_to_first_gold_read": turns_to_first_gold_read,
        "turns_to_first_test_run": turns_to_first_test,
        "wrong_branch_reads": wrong_branch,
        "wrong_branch_files": sorted(set(wrong_branch_files)),
        **counts,
        **receipts,
    }
    return report


# ===========================================================================
# AGGREGATE
# ===========================================================================
_AGG_NUMERIC = (
    "non_re_acquisition_rate", "searchable_delivered_fact_count",
    "re_acquisition_event_count", "re_acquired_fact_count",
    "turns_to_first_edit", "turns_to_first_gold_read", "turns_to_first_test_run",
    "wrong_branch_reads", "action_count", "search_count", "view_count",
    "edit_count", "test_count", "delivered_fact_total_distinct",
)


def aggregate(reports: list[dict]) -> dict:
    graded = [r for r in reports if r.get("trajectory_source") not in (None, "none")]
    agg = {
        "n_reports": len(reports),
        "n_graded": len(graded),
        "n_skipped": len(reports) - len(graded),
        "metrics": {},
    }
    # per-class fact totals across graded runs. The per-run delivered_fact_count key
    # is emitted under LEGACY class names (byte-stability); read + emit likewise.
    cls_tot = {_LEGACY_CLASS_ALIAS.get(c, c): 0 for c in FACT_CLASSES}
    for r in graded:
        for c in cls_tot:
            cls_tot[c] += int((r.get("delivered_fact_count") or {}).get(c, 0) or 0)
    agg["delivered_fact_count_total"] = cls_tot
    agg["fact_class_legacy_aliases"] = dict(_LEGACY_CLASS_ALIAS)
    for key in _AGG_NUMERIC:
        vals = [r.get(key) for r in graded if isinstance(r.get(key), (int, float))]
        if vals:
            agg["metrics"][key] = {
                "n": len(vals),
                "mean": round(statistics.fmean(vals), 8),
                "median": round(statistics.median(vals), 8),
                "min": round(min(vals), 8),
                "max": round(max(vals), 8),
            }
        else:
            agg["metrics"][key] = {"n": 0, "mean": None, "median": None,
                                   "min": None, "max": None}

    # receipt ADMIT/CUT input table: class -> highest-receipt-level distribution
    # (summed over graded runs) + cumulative at_least + 8dp rates. Missing denom=null.
    admit = {cls: {name: 0 for name in _RECEIPT_ORDER} for cls in FACT_CLASSES}
    weak_tot = 0                                   # F1 verify weak column
    ded = {name: 0 for name in _RECEIPT_ORDER}     # F3 deduped localization
    ded_groups = 0
    bounds = {"lower_entity_anchored": 0, "code_shaped_heuristic": 0,
              "loose_ceiling": 0}                  # F5 obligation bounds
    for r in graded:
        rbc = r.get("receipt_by_class") or {}
        for cls in FACT_CLASSES:
            dist = (rbc.get(cls) or {}).get("highest_level_distribution") or {}
            for name in _RECEIPT_ORDER:
                admit[cls][name] += int(dist.get(name, 0) or 0)
        weak_tot += int((rbc.get(VERIFY_NUDGE_CLS) or {})
                        .get("resolved_state_weak_any_green") or 0)
        d = (rbc.get("localization-file") or {}).get("deduped") or {}
        ded_groups += int(d.get("group_count", 0) or 0)
        for name in _RECEIPT_ORDER:
            ded[name] += int((d.get("highest_level_distribution") or {}).get(name, 0) or 0)
        b = (rbc.get("obligation") or {}).get("referenced_bounds") or {}
        for k in bounds:
            bounds[k] += int(b.get(k, 0) or 0)

    def _cume(dist: dict, n: int) -> tuple[dict, dict]:
        at_least: dict[str, int] = {}
        acc = 0
        for name in reversed(_RECEIPT_ORDER):
            acc += dist[name]
            at_least[name] = acc
        rate = {name: (round(at_least[name] / n, 8) if n else None)
                for name in _RECEIPT_ORDER}
        return ({name: at_least[name] for name in _RECEIPT_ORDER}, rate)

    table: dict[str, dict] = {}
    for cls in FACT_CLASSES:
        dist = admit[cls]
        n = sum(dist.values())
        at_least, rate = _cume(dist, n)
        entry = dict(dist)
        entry["delivered_distinct"] = n
        # B-7: every grade_run() is single-arm, so the aggregate ADMIT/CUT table is a
        # single-arm rollup — label it so its RESOLVED_STATE rate is never read as causal.
        entry["arms"] = ARMS_SINGLE
        entry["at_least"] = at_least
        entry["receipt_rate"] = rate
        entry["resolved_state_weak_any_green"] = None
        entry["rate_resolved_state_weak_any_green"] = None
        entry["deduped"] = None
        entry["referenced_bounds"] = None
        if cls == VERIFY_NUDGE_CLS:
            entry["resolved_state_weak_any_green"] = weak_tot
            entry["rate_resolved_state_weak_any_green"] = (
                round(weak_tot / n, 8) if n else None)
            entry["note"] = ("resolved_state = STRICT (green test NAMES the delivered "
                             "file) = the decision column; weak = any-green, "
                             "target-blind. ACTED~1.0 is tautological here (the token "
                             "is a file the agent was editing).")
        if cls == "localization-file":
            dal, drate = _cume(ded, ded_groups)
            entry["deduped"] = {
                "group_count": ded_groups,
                "highest_level_distribution": dict(ded),
                "at_least": dal,
                "rate": drate,
                "note": "ADMIT/CUT decisions cite the DEDUPED column (suffix-identity "
                        "variant groups; raw is variant-inflated).",
            }
        if cls == "obligation":
            entry["referenced_bounds"] = {
                **bounds,
                "rate_lower_entity_anchored": (
                    round(bounds["lower_entity_anchored"] / n, 8) if n else None),
                "rate_code_shaped_heuristic": (
                    round(bounds["code_shaped_heuristic"] / n, 8) if n else None),
                "rate_loose_ceiling": (
                    round(bounds["loose_ceiling"] / n, 8) if n else None),
                "note": ("REFERENCED for prose is a LOWER BOUND (entity-anchored); "
                         "code_shaped is HEURISTIC; loose_ceiling is the untightened "
                         "any-token upper bound."),
            }
        table[cls] = entry
    agg["receipt_admit_cut_table"] = table
    # B-7: the aggregate is a single-arm rollup; CAUSAL is unavailable at this granularity.
    agg["receipt_arms"] = ARMS_SINGLE
    agg["receipt_causal"] = CAUSAL_UNAVAILABLE_SINGLE_ARM
    agg["receipt_ladder_note"] = RECEIPT_LADDER_NOTE
    return agg


def _looks_like_run(d: str) -> bool:
    """A dir is gradable if it (recursively) holds a recognizable trajectory."""
    _, kind = _canonical_trajectory(d)
    return kind != "none"


def _iter_run_dirs(glob_pat: str) -> list[str]:
    """Directories under a glob that directly own a trajectory (dedup to the run root:
    the dir CONTAINING output.jsonl's results tree, or the tape dir holding jobs/)."""
    roots: set[str] = set()
    # output.jsonl runs: the run root is the artifact dir ABOVE results/
    for oj in _glob.glob(os.path.join(glob_pat, "output.jsonl"), recursive=True):
        # climb to the task-artifact root (…/task-XXX/…): use the dir 5 levels up if it
        # matches the known OH layout, else the immediate parent chain until 'results'.
        parts = _np(oj).split("/")
        if "results" in parts:
            roots.add("/".join(parts[:parts.index("results")]))
        else:
            roots.add(os.path.dirname(oj))
    # mini tapes: the run root is the dir holding jobs/**/agent/*.trajectory.json
    for tj in _glob.glob(os.path.join(glob_pat, "*.trajectory.json"), recursive=True):
        parts = _np(tj).split("/")
        if "jobs" in parts:
            roots.add("/".join(parts[:parts.index("jobs")]))
        else:
            roots.add(os.path.dirname(tj))
    return sorted(r for r in roots if r and os.path.isdir(r))


def _sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_receipt_table(corpora: dict[str, str], out_dir: str,
                      argv: list[str] | None = None) -> dict:
    """F9: the reproducible consumption-table driver (was an out-of-repo inline
    script). SUBSET RULE: for each named corpus root, every run dir found by
    _iter_run_dirs(root + '/**') (sorted, deterministic); graded runs only.
    Writes AGG_<name>.json per corpus + AGG_COMBINED.json + PROVENANCE.json.
    Deterministic: no timestamps in any payload."""
    os.makedirs(out_dir, exist_ok=True)
    all_reports: list[dict] = []
    per_corpus: dict[str, dict] = {}
    for name in sorted(corpora):
        root = corpora[name]
        run_dirs = _iter_run_dirs(root.rstrip("/\\") + "/**")
        reports = [grade_run(d) for d in run_dirs]
        graded = [r for r in reports if r["trajectory_source"] != "none"]
        per_corpus[name] = {"root": _np(root), "n_run_dirs": len(run_dirs),
                            "n_graded": len(graded)}
        with open(os.path.join(out_dir, f"AGG_{name}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(aggregate(reports), fh, indent=2, sort_keys=True, default=str)
        all_reports += graded
    combined = aggregate(all_reports)
    combined["subset_rule"] = (
        "named corpus roots; every run dir found by _iter_run_dirs(root + '/**'), "
        "sorted (deterministic); graded runs only")
    combined["per_corpus"] = per_corpus
    provenance = {
        "driver": "gt_substitution_grader.py --receipt-table",
        "argv": list(argv or []),
        "grader_sha256": _sha256_file(os.path.abspath(__file__)),
        "corpora": {n: _np(r) for n, r in sorted(corpora.items())},
        "out_dir": _np(out_dir),
    }
    combined["provenance"] = provenance
    with open(os.path.join(out_dir, "AGG_COMBINED.json"), "w", encoding="utf-8") as fh:
        json.dump(combined, fh, indent=2, sort_keys=True, default=str)
    with open(os.path.join(out_dir, "PROVENANCE.json"), "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2, sort_keys=True, default=str)
    return combined


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", nargs="?", help="a single run/tape directory")
    ap.add_argument("--gold", action="append", default=[], help="gold file (repeatable)")
    ap.add_argument("--glob", help="glob root; grade every run dir found beneath it")
    ap.add_argument("--out", help="write the single-run report JSON here")
    ap.add_argument("--out-dir", help="write per-run + AGGREGATE JSON here (glob mode)")
    ap.add_argument("--pair-on", help="GT-on run dir; with --pair-off => paired CAUSAL (B-7)")
    ap.add_argument("--pair-off", help="GT-off run dir (the matched pair for --pair-on)")
    ap.add_argument("--receipt-table", action="store_true",
                    help="F9 driver: grade --corpus roots, write AGG_* + PROVENANCE")
    ap.add_argument("--corpus", action="append", default=[], metavar="NAME=ROOT",
                    help="named corpus root for --receipt-table (repeatable)")
    args = ap.parse_args()

    if args.receipt_table:
        if not args.corpus or not args.out_dir:
            ap.error("--receipt-table needs --corpus NAME=ROOT (repeatable) + --out-dir")
        corpora: dict[str, str] = {}
        for spec in args.corpus:
            name, _, root = spec.partition("=")
            if not name or not root:
                ap.error(f"bad --corpus {spec!r}; expected NAME=ROOT")
            corpora[name] = root
        combined = run_receipt_table(corpora, args.out_dir, argv=sys.argv[1:])
        print(json.dumps({"per_corpus": combined["per_corpus"],
                          "n_graded": combined["n_graded"],
                          "out_dir": _np(args.out_dir)},
                         indent=2, sort_keys=True))
        return

    if args.pair_on or args.pair_off:
        # B-7: the ONLY path that emits a CAUSAL (level-5) verdict — a matched pair. A
        # single arm passed here (same dir for both, or one side omitted) is refused.
        if not (args.pair_on and args.pair_off):
            ap.error("--pair-on and --pair-off must be given together (a matched pair)")
        on_rep = grade_run(args.pair_on, args.gold or None)
        off_rep = grade_run(args.pair_off, args.gold or None)
        causal = compute_causal(on_rep, off_rep)   # raises SingleArmCausalError on 1 arm
        print(json.dumps(causal, indent=2, sort_keys=True, default=str))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(causal, fh, indent=2, sort_keys=True, default=str)
            print(f"\n[WROTE] {args.out}", file=sys.stderr)
        return

    if args.glob:
        run_dirs = _iter_run_dirs(args.glob)
        # also treat immediate children that look like runs (mini tapes without jobs/)
        reports = []
        readable = skipped = 0
        skip_reasons: dict[str, int] = {}
        for d in run_dirs:
            rep = grade_run(d, args.gold or None)
            if rep["trajectory_source"] == "none":
                skipped += 1
                skip_reasons["no_trajectory"] = skip_reasons.get("no_trajectory", 0) + 1
                continue
            readable += 1
            reports.append(rep)
            if args.out_dir:
                os.makedirs(args.out_dir, exist_ok=True)
                safe = _np(d).replace("/", "__").replace(":", "")
                with open(os.path.join(args.out_dir, f"subst_{safe}.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(rep, fh, indent=2, sort_keys=True, default=str)
        agg = aggregate(reports)
        agg["readable"] = readable
        agg["skipped"] = skipped
        agg["skip_reasons"] = skip_reasons
        agg["scanned_run_dirs"] = len(run_dirs)
        print(json.dumps(agg, indent=2, sort_keys=True, default=str))
        if args.out_dir:
            with open(os.path.join(args.out_dir, "subst_AGGREGATE.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(agg, fh, indent=2, sort_keys=True, default=str)
        return

    if not args.run_dir:
        ap.error("provide a run_dir, or --glob ROOT")
    rep = grade_run(args.run_dir, args.gold or None)
    print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, sort_keys=True, default=str)
        print(f"\n[WROTE] {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
