#!/usr/bin/env python3
"""Budget tracker for Vertex AI Qwen3-Coder SWE-bench runs.

Reads trajectory JSONL/JSON logs, computes per-task token costs, running total.
Vertex AI pricing for Qwen3-Coder (MaaS): $0 with GCP credits.
"""
import json
import sys
from pathlib import Path


# Vertex AI MaaS pricing (per 1M tokens) — update if pricing changes
INPUT_COST_PER_M = 0.30   # $/1M input tokens
OUTPUT_COST_PER_M = 0.60  # $/1M output tokens


def analyze_dir(results_dir: str) -> None:
    results_path = Path(results_dir)
    total_input = 0
    total_output = 0
    task_count = 0

    for condition in ["baseline", "gt"]:
        cond_path = results_path / condition
        if not cond_path.exists():
            continue

        cond_input = 0
        cond_output = 0
        cond_tasks = 0

        for phase in ["smoke", "full"]:
            phase_path = cond_path / phase
            if not phase_path.exists():
                continue
            for traj_file in phase_path.rglob("*.traj.json"):
                try:
                    with open(traj_file) as f:
                        data = json.load(f)
                    metrics = data.get("metrics", data.get("info", {}).get("metrics", {}))
                    inp = metrics.get("input_tokens", metrics.get("prompt_tokens", 0))
                    out = metrics.get("output_tokens", metrics.get("completion_tokens", 0))
                    cond_input += inp
                    cond_output += out
                    cond_tasks += 1
                except (json.JSONDecodeError, OSError):
                    continue

        cond_cost = (cond_input / 1_000_000 * INPUT_COST_PER_M +
                     cond_output / 1_000_000 * OUTPUT_COST_PER_M)
        print(f"  {condition}: {cond_tasks} tasks, "
              f"{cond_input:,} input + {cond_output:,} output tokens, "
              f"${cond_cost:.2f}")

        total_input += cond_input
        total_output += cond_output
        task_count += cond_tasks

    total_cost = (total_input / 1_000_000 * INPUT_COST_PER_M +
                  total_output / 1_000_000 * OUTPUT_COST_PER_M)
    print(f"\n  TOTAL: {task_count} tasks, "
          f"{total_input:,} input + {total_output:,} output tokens, "
          f"${total_cost:.2f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: budget_tracker.py <results_dir>")
        sys.exit(1)
    analyze_dir(sys.argv[1])
