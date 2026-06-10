"""Stage-2 FLIP metrics: pair GT-on results against the FROZEN baseline. No baseline re-run.

Per CLAUDE.md "BASELINE ALREADY EXISTS — NEVER RERUN IT": pair every GT-on result against
.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json (87/300).
  flip       = GT-on RESOLVES a task whose id is NOT in baseline resolved_ids (baseline=NO)
  regression = GT-on FAILS a task whose id IS in baseline resolved_ids (baseline=PASS)
Paired per-task delta (+1 flip / -1 regression / 0 same); paired sign test (Wilcoxon needs a
continuous metric — for binary resolve, the sign test on the delta is the right paired gate).
Emits 8-dp metrics. Reads RESOLVED from each task's eval_result.json (official eval), NEVER telemetry.

Usage: python scripts/drift/flip_metrics.py --artifacts <dir of task-*/ with eval_result.json>
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

_BASELINE = ".claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json"


def _baseline_ids() -> set[str]:
    return set(json.load(open(_BASELINE, encoding="utf-8"))["resolved_ids"])


def _resolved(task_dir: str) -> str:
    er = glob.glob(os.path.join(task_dir, "**", "eval_result.json"), recursive=True)
    if not er:
        return "no_eval"
    try:
        r = json.load(open(er[0], encoding="utf-8"))
    except Exception:
        return "no_eval"
    if r.get("resolved_instances") == 1 or r.get("resolved_ids"):
        return "RESOLVED"
    return "NO"


def _sign_test_p(flips: int, regressions: int) -> float:
    """Two-sided exact sign test p-value on the non-zero paired deltas (flips vs regressions)."""
    n = flips + regressions
    if n == 0:
        return 1.0
    k = min(flips, regressions)
    # P(X<=k) under Binom(n, 0.5), two-sided
    cum = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * cum)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, help="dir of task-<id>/ with eval_result.json")
    a = ap.parse_args(argv)
    base = _baseline_ids()
    task_dirs = sorted(d for d in glob.glob(os.path.join(a.artifacts, "*")) if os.path.isdir(d))

    flips, regressions, same, ungraded, rows = [], [], [], [], []
    for td in task_dirs:
        task = os.path.basename(td).replace("task-", "")
        res = _resolved(td)
        base_pass = task in base
        if res == "no_eval":
            ungraded.append(task); delta = None
        elif res == "RESOLVED" and not base_pass:
            flips.append(task); delta = 1
        elif res == "NO" and base_pass:
            regressions.append(task); delta = -1
        else:
            same.append(task); delta = 0
        rows.append({"task": task, "gt_on": res, "baseline_pass": base_pass, "delta": delta})

    graded = len(flips) + len(regressions) + len(same)
    metrics = {
        "graded_tasks": graded,
        "ungraded_tasks": len(ungraded),
        "flips": len(flips),
        "regressions": len(regressions),
        "net_flips": len(flips) - len(regressions),
        "flip_rate_over_graded": round(len(flips) / graded, 8) if graded else 0.0,
        "paired_sign_test_p": round(_sign_test_p(len(flips), len(regressions)), 8),
        "flip_ids": flips,
        "regression_ids": regressions,
    }
    print("=== Stage-2 FLIP metrics (GT-on paired vs frozen baseline 87/300) ===")
    print(json.dumps(metrics, indent=2))
    if ungraded:
        print(f"\nWARNING: {len(ungraded)} ungraded tasks (no eval_result.json) — NOT counted: {ungraded[:10]}")
    print("\nNOTE: a flip COUNTS only with a RIGHT TRAJECTORY (CLAUDE.md): verify GT delivered correct "
          "context the agent consumed en route to the fix — read each flip's output.jsonl before claiming it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
