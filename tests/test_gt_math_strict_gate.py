"""Pin the strict gt_math gate (scripts/gt_math_strict.py) — Fix 8 / Blocker D.

The gate exists so '—' (unmeasured), '⚪' (delivered-not-consumed), and '🔇' (silent) can
NEVER be counted as parity. These pins assert it NO-GOs (rc=1) on a run missing the C1/C2
witnesses and GOs (rc=0) only once the [GT_META] passage_window + sem_body witnesses are present.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "gt_math_strict.py"))


def _task(dirpath, *, log_lines, all_on=True, brief=True):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "trial_output.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    json.dump({"verdict": {"all_on": all_on}},
              open(os.path.join(dirpath, "foundational_gate_report.json"), "w"))
    msg1 = "<gt-task-brief> ..." if brief else "no brief here"
    json.dump({"messages": [{"content": "system"}, {"content": msg1}]},
              open(os.path.join(dirpath, "mini-swe-agent.trajectory.json"), "w"))


def _run(run_dir):
    p = subprocess.run([sys.executable, _SCRIPT, "--run-dir", run_dir, "--strict"],
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def test_nogo_when_c1_unmeasured(tmp_path):
    run = tmp_path / "run"
    _task(str(run / "ll-full-a__t1"), log_lines=["L6_REINDEX_OK", "[GT_RETRY] disabled"])
    rc, out = _run(str(run))
    assert rc == 1 and "NO-GO" in out and "C1 wide-passage UNMEASURED" in out


def test_nogo_when_leak_present(tmp_path):
    run = tmp_path / "run"
    d = str(run / "ll-full-a__t1")
    _task(d, log_lines=["[GT_META] passage_window window=256 wide=1 flag=1 e5=0 model=x",
                        "[GT_META] sem_body body_terms_rows=5 string_lit_rows=1 calls_rows=2"])
    # inject a leak into a gt-* block in the trajectory
    json.dump({"messages": [{"content": "system"},
                            {"content": "<gt-task-brief> FAIL_TO_PASS: tests/x::test_y </gt-task-brief>"}]},
              open(os.path.join(d, "mini-swe-agent.trajectory.json"), "w"))
    rc, out = _run(str(run))
    assert rc == 1 and "LEAKAGE" in out


def test_go_when_witnesses_present(tmp_path):
    run = tmp_path / "run"
    _task(str(run / "ll-full-a__t1"),
          log_lines=["[GT_META] passage_window window=256 wide=1 flag=1 e5=0 model=gte",
                     "[GT_META] sem_body body_terms_rows=812 string_lit_rows=140 calls_rows=690",
                     "L6_REINDEX_OK", "[GT_RETRY] enabled: retries=2 (total attempts=3)"])
    rc, out = _run(str(run))
    assert rc == 0 and "STRICT VERDICT: GO" in out


def test_go_when_c2_failclosed_is_measured(tmp_path):
    # A fail-closed body-less graph is a MEASURED (honest) outcome, not '—' -> not a blocker.
    run = tmp_path / "run"
    _task(str(run / "ll-full-a__t1"),
          log_lines=["[GT_META] passage_window window=128 wide=0 flag=0 e5=0 model=gte",
                     "[GT_WARN] GT_SEM_BODY=1 but graph has 0 body-channel rows (body_terms absent)"])
    rc, out = _run(str(run))
    assert rc == 0 and "GO" in out
