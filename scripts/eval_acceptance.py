"""Eval Acceptance Gate — determines if a feature meets ALL acceptance criteria.

Enforces the full engineering plan requirements:
1. Uplift above variance (absolute or CI-based)
2. Multi-model reproduction (≥2 model baselines)
3. Multi-benchmark-family reproduction (≥2 families)
4. Regression ceiling (pass-to-pass flip rate)
5. Repeated runs per config (statistical confidence)

Exit code 0 = ACCEPTED, exit code 1 = REJECTED.

Usage:
    python scripts/eval_acceptance.py --gt-results gt_results.json --baseline baseline_results.json
    python scripts/eval_acceptance.py --gt-results gt.json --baseline bl.json --config benchmarks/eval_matrix/config.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def load_config(config_path: str | None) -> dict:
    """Load acceptance rules from config, or use defaults."""
    defaults = {
        "min_uplift_absolute": 5,       # percentage points
        "min_uplift_with_ci": 3,        # percentage points (if CI excludes +2)
        "min_model_baselines": 2,
        "min_benchmark_families": 2,
        "max_regression_rate": 0.02,    # 2%
        "min_runs_per_config": 3,
    }
    if config_path and Path(config_path).exists():
        try:
            with open(config_path) as f:
                data = yaml.safe_load(f)
            rules = data.get("acceptance_rules", {})
            defaults.update(rules)
        except Exception:
            pass
    return defaults


def compute_regression_rate(gt_data: dict, bl_data: dict) -> float:
    """Compute pass-to-pass regression rate.

    A regression is a task that PASSED in baseline but FAILED with GT enabled.
    Rate = regressions / total_baseline_passes.
    """
    # Extract per-task results if available
    gt_tasks = {}
    bl_tasks = {}

    for run in gt_data.get("runs", []):
        for task in run.get("task_results", []):
            task_id = task.get("task_id", "")
            gt_tasks[task_id] = task.get("resolved", False)

    for run in bl_data.get("runs", []):
        for task in run.get("task_results", []):
            task_id = task.get("task_id", "")
            bl_tasks[task_id] = task.get("resolved", False)

    if not bl_tasks:
        return 0.0  # No task-level data available

    baseline_passes = sum(1 for v in bl_tasks.values() if v)
    if baseline_passes == 0:
        return 0.0

    regressions = sum(
        1 for task_id, bl_passed in bl_tasks.items()
        if bl_passed and not gt_tasks.get(task_id, False)
    )

    return regressions / baseline_passes


def main() -> None:
    parser = argparse.ArgumentParser(description="GT Eval Acceptance Gate")
    parser.add_argument("--gt-results", required=True, help="GT-enabled results JSON")
    parser.add_argument("--baseline", required=True, help="Baseline results JSON")
    parser.add_argument("--config", default="benchmarks/eval_matrix/config.yaml",
                        help="Config with acceptance rules")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from benchmarks.eval_matrix.variance_check import run_variance_matrix

    rules = load_config(args.config)
    min_uplift = rules["min_uplift_absolute"] / 100.0
    min_uplift_ci = rules["min_uplift_with_ci"] / 100.0
    min_models = rules["min_model_baselines"]
    min_families = rules["min_benchmark_families"]
    max_regression = rules["max_regression_rate"]
    min_runs = rules["min_runs_per_config"]

    gt_data = json.loads(Path(args.gt_results).read_text())
    bl_data = json.loads(Path(args.baseline).read_text())

    results = run_variance_matrix(gt_data, bl_data, min_uplift, min_runs)

    if not results:
        print("ERROR: No comparable results found")
        sys.exit(1)

    # ── Check 1: Multi-model reproduction ──
    passing_models: set[str] = set()
    passing_families: set[str] = set()
    failing: list[str] = []

    for r in results:
        passes_runs = r.min_samples_ok
        passes_uplift = r.exceeds_threshold or (
            r.uplift >= min_uplift_ci and r.ci_lower > 0.02
        )
        passes = passes_runs and passes_uplift and r.significant

        if args.verbose:
            status = "PASS" if passes else "FAIL"
            print(
                f"  {r.benchmark}/{r.model}: "
                f"uplift={r.uplift:+.1%}, t={r.t_statistic:.2f}, "
                f"ci=[{r.ci_lower:+.1%}, {r.ci_upper:+.1%}], "
                f"sig={r.significant}, exceeds={passes_uplift}, runs_ok={passes_runs} [{status}]"
            )

        if passes:
            passing_models.add(r.model)
            passing_families.add(r.benchmark)
        else:
            failing.append(f"{r.benchmark}/{r.model}")

    # ── Check 2: Multi-benchmark-family reproduction ──
    n_models = len(passing_models)
    n_families = len(passing_families)

    # ── Check 3: Regression ceiling ──
    regression_rate = compute_regression_rate(gt_data, bl_data)

    # ── Report ──
    print("\n=== GT Acceptance Gate ===")
    print(f"  Models passing:     {n_models}/{min_models} (need >= {min_models})")
    print(f"  Families passing:   {n_families}/{min_families} (need >= {min_families})")
    print(f"  Regression rate:    {regression_rate:.1%} (max {max_regression:.1%})")

    # ── Decision ──
    rejections: list[str] = []

    if n_models < min_models:
        rejections.append(
            f"Insufficient model baselines: {n_models} < {min_models}"
        )

    if n_families < min_families:
        rejections.append(
            f"Insufficient benchmark families: {n_families} < {min_families}"
        )

    if regression_rate > max_regression:
        rejections.append(
            f"Regression ceiling exceeded: {regression_rate:.1%} > {max_regression:.1%}"
        )

    insufficient_runs = [
        f"{r.benchmark}/{r.model}"
        for r in results
        if not r.min_samples_ok
    ]
    if insufficient_runs:
        rejections.append(
            f"Insufficient repeated runs for variance check (need >= {min_runs})"
        )

    if rejections:
        print(f"\nRESULT: REJECTED ({len(rejections)} rule(s) violated)")
        for r in rejections:
            print(f"  - {r}")
        if failing:
            print(f"  Failing configs: {', '.join(failing[:5])}")
        sys.exit(1)
    else:
        print("\nRESULT: ACCEPTED")
        print("  Uplift exceeds variance across sufficient models and benchmark families")
        print("  Regression rate within acceptable bounds")
        sys.exit(0)


if __name__ == "__main__":
    main()
