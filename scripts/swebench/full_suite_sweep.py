#!/usr/bin/env python3
"""Run EVERY test this repository owns, and report failures by NAME.

WHY THIS EXISTS
===============
On 2026-07-28 a change to ``_xsession_flush`` was landed and verified against
``tests/runtime``.  It was a live regression: 5 RED in
``artifact_deepswe/tests/test_xsession_seam_wiring_20260712.py`` and two
Profile-2 CAP members darkened.  The tree was never run.  The same session then
said "full suites green" three more times while executing 329 of the 985 test
files this repo owns -- 33%.

The failure was not carelessness about any one command; it was that "the full
suite" lived in habit rather than in code, so it drifted every time a new test
directory appeared.  This file is the fix: the sweep is now executable, its
coverage is asserted against a live census, and it fails loudly when a
directory exists that it does not run.

TWO ROOTS, NOT ONE
------------------
``tests/`` (858 files, 30 subdirectories) and ``artifact_deepswe/tests/`` (127).
The second is not reachable from the first by any pytest invocation -- it is a
sibling tree holding the live-seam wiring tests, and it is the one that gets
forgotten.

FAILURES ARE REPORTED BY NAME
-----------------------------
Never by count.  A count tells you the number moved; only a name tells you
whether the failure is yours.  Compare the printed name set against a baseline
captured the same way -- ``--baseline`` does exactly that and prints the three
sets that matter: NEW (yours), FIXED, and STILL-FAILING (pre-existing).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The roots holding every test this repository owns.  ``_assert_roots_cover``
#: proves at runtime that nothing lives outside them.
ROOTS = ("tests", "artifact_deepswe/tests")

#: Vendored/scratch trees holding test files that belong to OTHER projects
#: (benchmark checkouts, agent worktrees, demo repos).  Excluded from the
#: coverage census, never from a run -- no run ever descends into them.
#:
#: STRUCTURAL, NOT A DENYLIST.  The first version of this enumerated the
#: scratch directories it knew about and reported 973 "stray" files, because
#: ``.tmp_drift_work`` and ``.tmp_gt_architecture_audit`` had appeared since.
#: A denylist that must be edited whenever someone clones a benchmark repo is
#: the same drift this file exists to prevent, one level up.  Every dot-prefixed
#: directory is scratch or tooling by convention; the explicit names below are
#: the non-dotted vendored checkouts, which are few and stable.
_FOREIGN_EXACT = frozenset({
    "node_modules", "venv", "__pycache__", "gt-index", "django", "sympy",
    "demo-home-assistant-core", "localization_docs", "deepswe-pier",
    # Upstream harness checkout, not our source.
    "SWE-bench-Live",
    # The abandoned OpenHands scaffold.  CLAUDE.md already scopes it out of the
    # pre-dispatch gate (commit 72f0c848f); it is scoped out here for the same
    # reason -- it is not GT source and its 87/300 baseline is frozen.
    "gen_lab",
    # Pre-generated GT artifacts, an archived worktree, vendored scenario
    # fixtures, and the report tree -- none are GT source.
    "pregen_gt", "zz_wt_w2300", "test_scenarios", "reports",
})

def _is_collectable_test(path: pathlib.Path) -> int:
    """Count what pytest would actually collect from ``path``.

    The ``test_*.py`` NAME is not the discriminator.  This repo ships production
    modules that match it and define no tests at all --
    ``src/groundtruth/runtime/test_runner.py`` is the test-EXECUTION engine,
    ``src/groundtruth/pretask/test_link.py`` is a linker, and
    ``scripts/swebench/test_deepseek.py`` / ``test_vertex_regions.py`` are manual
    probes.  Flagging those as unrun tests is a false alarm that trains the reader
    to ignore this report.

    PARSE, DO NOT PATTERN-MATCH.  The first version used a
    ``^(?:def test_|class Test)`` regex and false-positived on
    ``scripts/analysis/test_command_classifier.py``, which is a classifier for test
    COMMANDS: it defines a ``TestTarget`` dataclass with zero test methods and three
    plain functions.  ``class Test`` in a file named ``test_*`` looks conclusive and
    isn't.  Counting ``def test_*`` at module level plus inside classes is what
    pytest actually collects, so it cannot be fooled by a type's name.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return 0
    n = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n += node.name.startswith("test")
        elif isinstance(node, ast.ClassDef):
            n += sum(
                isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name.startswith("test")
                for m in node.body
            )
    return n


def _is_foreign(dirname: str) -> bool:
    return dirname.startswith(".") or dirname in _FOREIGN_EXACT

#: Repo-owned test files that live OUTSIDE both roots and are nonetheless run.
#: Surfaced by the census on 2026-07-28 -- 43 test functions that no verification in
#: this repo had ever executed.  Each was READ before being added here.
EXTRA_TESTS = (
    "artifact_deepswe/test_gt_pro_witness.py",              # A3 Pro certify->consume witness
    "benchmarks/swebench/test_autocorrect.py",              # unittest, pure string/levenshtein
    "benchmarks/swebench/test_gt_intel_lipi_squash.py",     # sqlite3 + tempfile, hermetic
    "scripts/swebench/test_industrial_sota_validation_gate.py",  # hashlib + tempfile, hermetic
)

#: Files named ``test_*`` that are NOT unit suites.  Excluded from both the census
#: and the run, each with a reason -- the point of the census is to make omissions
#: deliberate, not to make the list empty.
NOT_A_SUITE = {
    # A standalone LIVE-environment verification script, not a unit suite: it shells
    # out (subprocess/docker) and its "tests" are probes -- test_watcher_running,
    # test_hook_runs, test_evidence_on_edit. Running it in a sweep would report
    # infrastructure absence as test failure.
    "scripts/swebench/test_gt_delivery_proof.py": "live-environment probe, not a unit suite",
}

#: Tests that hang this environment rather than fail.  Each needs a REASON and
#: an owning task -- an unexplained entry here is a silently skipped test.
DESELECT = {
    # Spawns a real pytest subprocess via covering_runner._differential_
    # attribution_result -> test_runner._run_subprocess, which never returns
    # on Windows.  Not a product defect; an environment one.
    "tests/runtime/test_b1_differential_attribution.py": "hangs: real pytest subprocess",
}

_OUTCOME_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def _assert_roots_cover() -> list[str]:
    """Fail loudly if a test file exists outside ``ROOTS``.

    This is the anti-drift mechanism.  A new top-level test directory is a
    normal thing to add and an easy thing to forget; this turns forgetting it
    into a hard error on the very next sweep instead of a silent 33%.

    PRUNE the walk, do not filter after it.  A plain ``rglob`` over this repo
    descends into vendored benchmark checkouts (``.tmp_phase0`` alone holds
    ~5000 test files) and does not finish inside five minutes.
    """
    stray: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if not _is_foreign(d)]
        rel_dir = pathlib.Path(dirpath).relative_to(REPO).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        if rel_dir and any(
            rel_dir == root or rel_dir.startswith(root + "/") for root in ROOTS
        ):
            continue  # inside a declared root: covered by definition
        for name in filenames:
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in EXTRA_TESTS or rel in NOT_A_SUITE:
                continue  # accounted for: run explicitly, or excluded with a reason
            n = _is_collectable_test(pathlib.Path(dirpath) / name)
            if n:
                stray.append(f"{rel}  [{n} test defs]")
    return stray


def _all_test_files() -> list[str]:
    """Every test file the sweep owns: both roots, plus the explicit extras."""
    files: list[str] = []
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("test_*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel in DESELECT or rel in NOT_A_SUITE:
                continue
            files.append(rel)
    files.extend(EXTRA_TESTS)
    return files


def _run(root: str, extra: list[str], *, targets: "list[str] | None" = None) -> tuple[set[str], str]:
    """Run one root (or an explicit target list); return failing node ids + summary."""
    cmd = [
        sys.executable, "-m", "pytest", *(targets or [root]), "-q", "--tb=no",
        "-p", "no:randomly",
        # WITHOUT THIS THE `tests/` ROOT RUNS ZERO TESTS.  A single module that
        # raises at import time (today: tests/layers/test_l5_gate.py,
        # FileNotFoundError) makes pytest print "Interrupted: 1 error during
        # collection" and abandon the whole root.  That is the mechanical reason
        # every sweep in this repo degenerated to `pytest tests/runtime`:
        # `pytest tests` has never worked.  Collection errors still surface --
        # _OUTCOME_RE matches ERROR lines, so they are counted as failures by
        # name -- but they no longer suppress the other 850 files.
        "--continue-on-collection-errors",
    ]
    for target, _reason in DESELECT.items():
        if target.startswith(root + "/"):
            cmd += ["--deselect", target]
    cmd += extra
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    failed = {
        m.group(2) for line in out.splitlines() if (m := _OUTCOME_RE.match(line.strip()))
    }
    # A file whose tests all SKIP, or that collects nothing, has neither " passed" nor
    # " failed" in its summary -- in per-file isolate mode that is normal, not a crash.
    summary = next(
        (ln for ln in reversed(out.splitlines())
         if any(k in ln for k in (" passed", " failed", " skipped", " error",
                                  "no tests ran"))),
        "",
    )
    if not summary:
        # NEVER swallow this.  A root that produces no summary line did not run:
        # a collection error, an INTERNALERROR, or a crash.  The first version
        # printed "<no summary line>" and carried on to print a total, which
        # would have been reported as a clean baseline while one whole root
        # contributed nothing.  A verification tool that hides its own failure
        # is worse than no tool.
        print(f"\n  !! {root}: pytest produced NO summary line (exit {proc.returncode}).")
        print("  !! This root did NOT run. Last 40 lines of its output:")
        for line in out.splitlines()[-40:]:
            print(f"     | {line}")
        raise SystemExit(f"sweep aborted: {root} did not run")
    return failed, summary.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", type=pathlib.Path,
                    help="JSON from a previous --save; diffs failure NAMES against it")
    ap.add_argument("--save", type=pathlib.Path,
                    help="write this run's failing node ids for a later --baseline")
    ap.add_argument("--isolate", action="store_true",
                    help="run every test FILE in its own pytest process. SLOW (~985 "
                         "processes) but the only trustworthy absolute number: run "
                         "together, this suite manufactures hundreds of phantom "
                         "failures through cross-test pollution -- see below.")
    ap.add_argument("pytest_args", nargs="*", default=[],
                    help="extra args forwarded to every pytest invocation")
    args = ap.parse_args()

    # REPORT, THEN RUN.  An earlier version returned here, and the first real
    # invocation executed ZERO tests because six known-unrun files existed.  A
    # sweep that refuses to run until an unrelated triage lands is a sweep
    # nobody uses -- it sends the reader straight back to ad-hoc pytest, which
    # is precisely the drift this file exists to prevent.  The defect is loud
    # and it poisons the exit code; it does not block the run.
    stray = _assert_roots_cover()
    if stray:
        print("SWEEP COVERAGE DEFECT -- collectable test files outside every declared root.")
        print("These are NOT run below, so the result is a partial verification:")
        for s in sorted(stray)[:40]:
            print(f"    {s}")
        print("\n  Add the directory to ROOTS if this repo owns those tests,"
              "\n  or to _FOREIGN_EXACT if it is a vendored checkout.\n")

    if DESELECT:
        print("DESELECTED (not run -- each needs a reason and an owner):")
        for target, reason in DESELECT.items():
            print(f"    {target}\n        {reason}")
        print()

    if NOT_A_SUITE:
        print("NOT A UNIT SUITE (excluded deliberately, with a reason):")
        for target, reason in NOT_A_SUITE.items():
            print(f"    {target}\n        {reason}")
        print()

    failing: set[str] = set()
    if args.isolate:
        # PER-FILE ISOLATION. Measured 2026-07-28: running `tests/` as one process
        # produced 500 failures at pristine HEAD, and three of four sampled offenders
        # PASSED completely on their own -- tests/unit/test_store.py 37 failed -> 38
        # passed, test_miniswe_canonical_event_fabric_wave5.py 30 -> 31 passed,
        # tests/unit/test_graph.py 13 -> 15 passed. Only
        # tests/test_gt_substitution_grader.py failed identically both ways. So the
        # aggregate number is dominated by cross-test pollution, not by broken code,
        # and it is not a usable baseline.
        #
        # A whole-root diff can still be read like-for-like when BOTH sides run the
        # same ordering -- pollution is deterministic under `-p no:randomly` and
        # largely cancels. But adding or removing a test file shifts collection order
        # and therefore shifts the pollution, so that cancellation is an assumption,
        # not a guarantee. When the absolute number matters, isolate.
        for target in _all_test_files():
            found, summary = _run("", args.pytest_args, targets=[target])
            failing |= found
            if "failed" in summary or "error" in summary.lower():
                print(f"  {target}\n      {summary}")
        print(f"\n  [isolated] {len(failing)} failing node id(s)")
    else:
        for root in ROOTS:
            found, summary = _run(root, args.pytest_args)
            print(f"  {root:26s} {summary}")
            failing |= found
    if EXTRA_TESTS:
        found, summary = _run("", args.pytest_args, targets=list(EXTRA_TESTS))
        print(f"  {'(extra files)':26s} {summary}")
        failing |= found

    print(f"\n  {len(failing)} failing node id(s) across {len(ROOTS)} root(s)")

    if args.save:
        args.save.write_text(json.dumps(sorted(failing), indent=2), encoding="utf-8")
        print(f"  saved -> {args.save}")

    if args.baseline and args.baseline.exists():
        base = set(json.loads(args.baseline.read_text(encoding="utf-8")))
        new, fixed, still = failing - base, base - failing, failing & base
        print(f"\n  NEW (attributable to this diff): {len(new)}")
        for n in sorted(new):
            print(f"      + {n}")
        print(f"  FIXED: {len(fixed)}")
        for n in sorted(fixed):
            print(f"      - {n}")
        print(f"  STILL FAILING (pre-existing, not yours): {len(still)}")
        if stray:
            print(f"\n  ...but coverage was PARTIAL ({len(stray)} file(s) unrun) -- see above.")
            return 2
        return 1 if new else 0

    if stray:
        return 2  # ran, but coverage was partial -- never report this as green
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
