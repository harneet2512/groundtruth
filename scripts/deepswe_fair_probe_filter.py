#!/usr/bin/env python3
"""DeepSWE self-localization (fair-probe) pre-filter — META ONLY, never GT product logic.

CLUSTER E / E3.  This script answers ONE question for RUN SELECTION:
    "Does the task's instruction.md already pre-name the gold the agent must edit?"
If it does, the agent can self-localize straight from the prompt and a `RESOLVED`
verdict is causally empty for GroundTruth — GT cannot be credited (gt_trial.md §4/§5
`fair_probe`; project memory: "Self-localizing issues can't prove GT causation — pick
baseline-fails ids").  We use it to PICK the LOW-coverage (fair-probe) tasks to run and
to flag the HIGH-coverage (self-localizing) ones to avoid.

  gold_name_coverage = fraction of GOLD NAMES that string-appear in instruction.md
    GOLD NAMES  =  basenames of the files solution.patch edits
                +  the NEW top-level symbols solution.patch adds (def/class/func/fn/...)
  A name "appears" if it occurs as a token in instruction.md (case-sensitive substring
  for symbols; basename or stem for files).  coverage in [0,1]; HIGH => self-localizing.

HARD BOUNDARY (why this is not benchmaxxing):
  * It reads solution.patch (the GOLD).  That is allowed HERE because this is run
    SELECTION metadata, computed OFFLINE, BEFORE the agent runs.  It is NEVER imported
    by, called from, or surfaced to any GT product module (brief / localizer / oracle /
    hooks).  No task-id / gold label ever crosses into product logic.  It only tells the
    operator which task IDs are fair probes.
  * It is fully generalized: any repo/language with the deepswe task layout
    (instruction.md + solution/solution.patch + task.toml) is scored by the same rule;
    the symbol extractors are per-language token grammars, not per-task patterns.

Usage:
  python scripts/deepswe_fair_probe_filter.py <tasks_dir> [--threshold 0.70] [--json out.json]
  # default tasks_dir: D:/Groundtruth/deepswe-bench/tasks  (override as arg)

Output: a ranked table (highest coverage first = most self-localizing) + a SELF-LOCALIZING
flag for coverage >= threshold, and the LOW-coverage fair-probe tail to pick runs from.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ---- gold-symbol extractors, per language (token grammars, not task patterns) -----------
# Each returns the set of NEW top-level symbol names introduced by ADDED ('+') patch lines.
# We deliberately take only DEFINITION lines (a new def/class/func/...), because those are
# the names an agent would have to INVENT vs. could LOOK UP — the discriminating signal for
# "did the prompt pre-name the solution".

_PY_DEF = re.compile(r"^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_GO_DEF = re.compile(r"^\+\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)")
_GO_TYPE = re.compile(r"^\+\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface|func|\w)")
_RS_DEF = re.compile(
    r"^\+\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
    r"(?:fn|struct|enum|trait|type|const|static|macro_rules!)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
# JS/TS: function/class/interface/type/enum declarations + top-level const/let bindings that
# are assigned a function/arrow/class (the "named export" idiom in the deepswe TS/JS gold).
_JS_DECL = re.compile(
    r"^\+\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function\*?|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)
_JS_BIND = re.compile(
    r"^\+\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[:=]"
)

# language-agnostic fallback: any "<keyword> Name" definition-ish added line.
_GENERIC_DEF = re.compile(
    r"^\+\s*(?:export\s+|pub\s+|public\s+|private\s+|static\s+|async\s+)*"
    r"(?:def|class|func|fn|function|interface|trait|struct|enum|type|object|module)\s+"
    r"([A-Za-z_$][A-Za-z0-9_$]*)"
)


def _extract_added_symbols(patch_text: str, language: str) -> set[str]:
    """New top-level symbol names introduced by added ('+') lines of solution.patch."""
    syms: set[str] = set()
    lang = (language or "").lower()
    for line in patch_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if lang == "python":
            for rx in (_PY_DEF,):
                m = rx.match(line)
                if m:
                    syms.add(m.group(1))
        elif lang == "go":
            for rx in (_GO_DEF, _GO_TYPE):
                m = rx.match(line)
                if m:
                    syms.add(m.group(1))
        elif lang == "rust":
            m = _RS_DEF.match(line)
            if m:
                syms.add(m.group(1))
        elif lang in ("javascript", "typescript"):
            for rx in (_JS_DECL, _JS_BIND):
                m = rx.match(line)
                if m:
                    syms.add(m.group(1))
        else:
            m = _GENERIC_DEF.match(line)
            if m:
                syms.add(m.group(1))
    # Always also run the generic extractor as a union safety net (covers mixed-language
    # patches and keeps the score from UNDER-counting gold names -> stays conservative,
    # i.e. never falsely calls a self-localizing task a fair probe).
    for line in patch_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            m = _GENERIC_DEF.match(line)
            if m:
                syms.add(m.group(1))
    # Drop trivial 1-char names (loop vars / noise) — they pollute coverage on both sides.
    return {s for s in syms if len(s) >= 2}


def _is_public_symbol(name: str) -> bool:
    """Is this a PUBLIC-surface symbol an issue would name to localize?

    Private/internal helpers are, by universal convention, "implementation details" that a
    spec does NOT pre-name (the fastapi instruction.md says exactly this: "Internal helper
    names ... are implementation details").  Scoring over them DILUTES the self-localization
    signal with names no prompt could be expected to mention.  The discriminating gold-name
    set is the PUBLIC surface: the names that actually pin localization.

    Language-agnostic proxy for "private": a leading underscore (Python `_x`/`__x__`, JS/TS
    `_x` idiom).  This is conservative — it only ever REMOVES names from the denominator, so
    it can lift a truly self-localizing task above threshold but never falsely flags a
    fair-probe task (a fair probe has low coverage on the public surface too).  Dunders
    (`__init__`/`__call__`) are framework hooks, never spec-named — always excluded.
    """
    if name.startswith("_"):
        return False
    return True


_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_PLUS_FILE = re.compile(r"^\+\+\+ b/(\S+)")


def _extract_gold_files(patch_text: str) -> list[str]:
    """Paths the solution.patch edits (repo-relative)."""
    files: list[str] = []
    for line in patch_text.splitlines():
        m = _DIFF_FILE.match(line)
        if m:
            files.append(m.group(2))
            continue
        m = _PLUS_FILE.match(line)
        if m and m.group(1) != "/dev/null":
            if m.group(1) not in files:
                files.append(m.group(1))
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _read_language(task_dir: str) -> str:
    toml = os.path.join(task_dir, "task.toml")
    try:
        with open(toml, encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("language"):
                    # language = "go"
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _name_in_instruction(name: str, instr: str) -> bool:
    """A symbol name 'appears' if it occurs as a bounded token in instruction.md."""
    return re.search(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])", instr) is not None


_COMMON_FILE_STEMS = frozenset(
    {
        # generic domain words that are also frequent module basenames — a bare match of
        # these in prose is almost always the CONCEPT, not a file reference. Excluding them
        # from the stem fallback prevents false self-localizing credit (e.g. "cache entry"
        # in a behavior spec naming cache.go).
        "cache", "config", "utils", "util", "main", "index", "core", "base", "client",
        "server", "types", "errors", "error", "common", "helpers", "helper", "module",
        "modules", "model", "models", "state", "store", "queue", "router", "routes",
        "test", "tests", "init", "data", "schema", "api", "app", "lib", "src", "result",
    }
)


def _file_in_instruction(path: str, instr: str) -> bool:
    """A gold file 'appears' if its basename (with extension) is named in instruction.md.

    The reliable signal is the basename WITH its extension (`routing.py`, `methods.go`) —
    an unambiguous file reference, which is exactly how the self-localizing deepswe tasks
    name their gold ("audit `applications.py` and `routing.py`").  A bare stem match is a
    false-positive magnet (the word "cache" in a behavior spec is the concept, not
    cache.go), so the stem fallback fires ONLY for long, distinctive stems that are not
    common domain words — correct-or-quiet on the file axis.
    """
    base = os.path.basename(path)
    if base and _name_in_instruction(base, instr):
        return True
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if (
        stem
        and len(stem) >= 6
        and stem.lower() not in _COMMON_FILE_STEMS
        and _name_in_instruction(stem, instr)
    ):
        return True
    return False


def score_task(task_dir: str) -> dict | None:
    instr_p = os.path.join(task_dir, "instruction.md")
    patch_p = os.path.join(task_dir, "solution", "solution.patch")
    if not (os.path.isfile(instr_p) and os.path.isfile(patch_p)):
        return None
    with open(instr_p, encoding="utf-8", errors="replace") as fh:
        instr = fh.read()
    with open(patch_p, encoding="utf-8", errors="replace") as fh:
        patch = fh.read()

    language = _read_language(task_dir)
    gold_files = _extract_gold_files(patch)
    all_syms = _extract_added_symbols(patch, language)
    # PRIMARY denominator = the PUBLIC surface (files + non-underscore symbols). Private
    # helpers are universally "implementation details" no spec pre-names — counting them
    # masks self-localization. We keep the full-set coverage as a secondary diagnostic.
    pub_syms = {s for s in all_syms if _is_public_symbol(s)}

    files_named = [f for f in gold_files if _file_in_instruction(f, instr)]
    pub_syms_named = sorted(s for s in pub_syms if _name_in_instruction(s, instr))
    all_syms_named = sorted(s for s in all_syms if _name_in_instruction(s, instr))

    # PRIMARY coverage — over the public surface.
    n_total = len(gold_files) + len(pub_syms)
    n_named = len(files_named) + len(pub_syms_named)
    coverage = (n_named / n_total) if n_total else 0.0

    # component coverages (so the operator can see WHICH axis is pre-named)
    file_cov = (len(files_named) / len(gold_files)) if gold_files else 0.0
    sym_cov = (len(pub_syms_named) / len(pub_syms)) if pub_syms else 0.0
    # secondary: coverage over ALL added symbols incl. private (diagnostic only)
    all_total = len(gold_files) + len(all_syms)
    all_named = len(files_named) + len(all_syms_named)
    coverage_all = (all_named / all_total) if all_total else 0.0

    return {
        "task_id": os.path.basename(os.path.normpath(task_dir)),
        "language": language,
        "gold_files": gold_files,
        "gold_files_named": files_named,
        "n_gold_files": len(gold_files),
        "n_gold_symbols_public": len(pub_syms),
        "n_gold_symbols_all": len(all_syms),
        "gold_symbols_named": pub_syms_named,
        "file_coverage": round(file_cov, 8),
        "symbol_coverage": round(sym_cov, 8),
        "gold_name_coverage": round(coverage, 8),
        "gold_name_coverage_all_symbols": round(coverage_all, 8),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "tasks_dir",
        nargs="?",
        default=r"D:/Groundtruth/deepswe-bench/tasks",
        help="deepswe-bench tasks/ directory (default: D:/Groundtruth/deepswe-bench/tasks)",
    )
    ap.add_argument("--threshold", type=float, default=0.70, help="coverage >= this is flagged SELF-LOCALIZING")
    ap.add_argument("--json", default="", help="write the full ranked records to this JSON path")
    ap.add_argument("--top", type=int, default=15, help="how many top/bottom rows to print")
    args = ap.parse_args(argv)

    tasks_dir = args.tasks_dir
    if not os.path.isdir(tasks_dir):
        print(f"ERROR: tasks dir not found: {tasks_dir}", file=sys.stderr)
        return 2

    records = []
    for name in sorted(os.listdir(tasks_dir)):
        td = os.path.join(tasks_dir, name)
        if not os.path.isdir(td):
            continue
        rec = score_task(td)
        if rec is not None:
            rec["self_localizing"] = rec["gold_name_coverage"] >= args.threshold
            records.append(rec)

    records.sort(key=lambda r: (-r["gold_name_coverage"], r["task_id"]))

    n = len(records)
    n_self = sum(1 for r in records if r["self_localizing"])
    n_fair = n - n_self
    print(f"# DeepSWE fair-probe pre-filter — {n} tasks, threshold={args.threshold:.2f}")
    print(f"# SELF-LOCALIZING (>= thr): {n_self}    FAIR-PROBE (< thr): {n_fair}")
    print()
    hdr = f"{'coverage':>9}  {'file':>5}  {'sym':>5}  {'lang':<11} {'flag':<14} task_id"
    print(hdr)
    print("-" * len(hdr))

    def _row(r: dict) -> str:
        flag = "SELF-LOCALIZE" if r["self_localizing"] else "fair-probe"
        return (
            f"{r['gold_name_coverage']:>9.4f}  "
            f"{r['file_coverage']:>5.2f}  {r['symbol_coverage']:>5.2f}  "
            f"{r['language']:<11} {flag:<14} {r['task_id']}"
        )

    print(f"## TOP {args.top} (most self-localizing — AVOID as GT probes)")
    for r in records[: args.top]:
        print(_row(r))
    print()
    print(f"## BOTTOM {args.top} (lowest coverage — the FAIR-PROBE pool to run GT on)")
    for r in records[-args.top:]:
        print(_row(r))

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(
                {"threshold": args.threshold, "n_tasks": n, "n_self_localizing": n_self,
                 "n_fair_probe": n_fair, "tasks": records},
                fh, indent=2,
            )
        print(f"\n# wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
