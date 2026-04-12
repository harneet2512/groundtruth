"""Variance Check — repeated-run confidence intervals.

Determines whether observed uplift exceeds model variance by running
statistical tests on repeated evaluation runs.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass
class VarianceResult:
    """Result of a variance check between GT-enabled and baseline runs."""

    benchmark: str
    model: str
    gt_mean: float
    baseline_mean: float
    uplift: float
    gt_std: float
    baseline_std: float
    pooled_std: float
    t_statistic: float
    ci_lower: float
    ci_upper: float
    min_samples_ok: bool
    significant: bool
    """True if uplift exceeds variance at 95% confidence."""

    exceeds_threshold: bool
    """True if uplift ≥ min_uplift_absolute."""


def check_variance(
    gt_rates: list[float],
    baseline_rates: list[float],
    min_uplift: float = 0.05,
    min_runs: int = 3,
) -> VarianceResult:
    """Compare GT-enabled vs baseline using Welch's t-test.

    Args:
        gt_rates: Resolved rates from GT-enabled runs.
        baseline_rates: Resolved rates from baseline runs.
        min_uplift: Minimum absolute uplift to consider meaningful (default 5%).

    Returns VarianceResult with statistical significance.
    """
    n_gt = len(gt_rates)
    n_bl = len(baseline_rates)

    if n_gt < 2 or n_bl < 2:
        return VarianceResult(
            benchmark="",
            model="",
            gt_mean=statistics.mean(gt_rates) if gt_rates else 0.0,
            baseline_mean=statistics.mean(baseline_rates) if baseline_rates else 0.0,
            uplift=0.0,
            gt_std=0.0,
            baseline_std=0.0,
            pooled_std=0.0,
            t_statistic=0.0,
            ci_lower=0.0,
            ci_upper=0.0,
            min_samples_ok=False,
            significant=False,
            exceeds_threshold=False,
        )

    gt_mean = statistics.mean(gt_rates)
    bl_mean = statistics.mean(baseline_rates)
    gt_std = statistics.stdev(gt_rates)
    bl_std = statistics.stdev(baseline_rates)

    uplift = gt_mean - bl_mean

    # Welch's t-test
    se_gt = gt_std / math.sqrt(n_gt)
    se_bl = bl_std / math.sqrt(n_bl)
    pooled_se = math.sqrt(se_gt**2 + se_bl**2)
    pooled_std = math.sqrt((gt_std**2 + bl_std**2) / 2)

    if pooled_se > 0:
        t_stat = uplift / pooled_se
    else:
        t_stat = 0.0

    ci_margin = 1.96 * pooled_se if pooled_se > 0 else 0.0
    ci_lower = uplift - ci_margin
    ci_upper = uplift + ci_margin
    min_samples_ok = n_gt >= min_runs and n_bl >= min_runs

    # Approximate 95% significance.
    significant = abs(t_stat) > 2.0 and uplift > 0
    exceeds_threshold = uplift >= min_uplift

    return VarianceResult(
        benchmark="",
        model="",
        gt_mean=gt_mean,
        baseline_mean=bl_mean,
        uplift=uplift,
        gt_std=gt_std,
        baseline_std=bl_std,
        pooled_std=pooled_std,
        t_statistic=t_stat,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        min_samples_ok=min_samples_ok,
        significant=significant,
        exceeds_threshold=exceeds_threshold,
    )


def run_variance_matrix(
    gt_results: dict,
    baseline_results: dict,
    min_uplift: float = 0.05,
    min_runs: int = 3,
) -> list[VarianceResult]:
    """Run variance check across all benchmark/model combinations.

    Args:
        gt_results: Matrix results with GT enabled.
        baseline_results: Matrix results without GT (baseline).
        min_uplift: Minimum absolute uplift threshold.

    Returns list of VarianceResults.
    """
    # Group by (benchmark, model)
    gt_groups: dict[tuple[str, str], list[float]] = {}
    bl_groups: dict[tuple[str, str], list[float]] = {}

    for run in gt_results.get("runs", []):
        key = (run["benchmark"], run["model"])
        gt_groups.setdefault(key, []).append(run["resolved_rate"])

    for run in baseline_results.get("runs", []):
        key = (run["benchmark"], run["model"])
        bl_groups.setdefault(key, []).append(run["resolved_rate"])

    results: list[VarianceResult] = []
    all_keys = set(gt_groups.keys()) | set(bl_groups.keys())

    for bench, model in sorted(all_keys):
        gt_rates = gt_groups.get((bench, model), [])
        bl_rates = bl_groups.get((bench, model), [])

        if not gt_rates or not bl_rates:
            continue

        var_result = check_variance(gt_rates, bl_rates, min_uplift, min_runs)
        # Fill in benchmark/model
        results.append(VarianceResult(
            benchmark=bench,
            model=model,
            gt_mean=var_result.gt_mean,
            baseline_mean=var_result.baseline_mean,
            uplift=var_result.uplift,
            gt_std=var_result.gt_std,
            baseline_std=var_result.baseline_std,
            pooled_std=var_result.pooled_std,
            t_statistic=var_result.t_statistic,
            ci_lower=var_result.ci_lower,
            ci_upper=var_result.ci_upper,
            min_samples_ok=var_result.min_samples_ok,
            significant=var_result.significant,
            exceeds_threshold=var_result.exceeds_threshold,
        ))

    return results
