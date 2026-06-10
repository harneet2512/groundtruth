"""Post-run verifier for the Track D Lite localization run.

Asserts:
  - tasks_completed == expected (300 for full Lite)
  - error_rate < 0.05
  - gold_files extracted for >=95% of tasks
  - mean func_hit_at_5 > 0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True)
    p.add_argument("--expected-tasks", type=int, default=300)
    args = p.parse_args()

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    per_task = data.get("per_task") or []
    aggregate = data.get("aggregate") or {}

    failures: list[str] = []

    tasks_completed = aggregate.get("task_count", len(per_task))
    if tasks_completed != args.expected_tasks:
        failures.append(
            f"tasks_completed={tasks_completed} != expected={args.expected_tasks}"
        )

    n = max(tasks_completed, 1)
    err_count = aggregate.get(
        "error_count", sum(1 for r in per_task if "error" in r)
    )
    error_rate = err_count / n
    if error_rate >= 0.05:
        failures.append(f"error_rate={error_rate:.3f} >= 0.05 ({err_count} errors)")

    with_gold_files = sum(1 for r in per_task if r.get("gold_files"))
    gold_extract_rate = with_gold_files / n
    if gold_extract_rate < 0.95:
        failures.append(
            f"gold_extract_rate={gold_extract_rate:.3f} < 0.95 "
            f"({with_gold_files}/{n} have gold_files)"
        )

    func_hits = sum(1 for r in per_task if r.get("hit_func_at_5"))
    mean_func_hit_at_5 = func_hits / n
    if mean_func_hit_at_5 <= 0:
        failures.append("mean_func_hit_at_5 == 0 (function ranker not firing)")

    print("=" * 60)
    print("Track D Lite localization verifier")
    print("=" * 60)
    print(f"tasks_completed:       {tasks_completed} / {args.expected_tasks}")
    print(f"error_rate:            {error_rate:.4f} ({err_count} errors)")
    print(f"gold_extract_rate:     {gold_extract_rate:.4f}")
    print(f"mean_func_hit_at_5:    {mean_func_hit_at_5:.4f}")
    print(f"acc_file_at_5:         {aggregate.get('acc_file_at_5', 0.0):.4f}")
    print(f"acc_func_at_10:        {aggregate.get('acc_func_at_10', 0.0):.4f}")
    print()
    if failures:
        print("VERDICT: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
