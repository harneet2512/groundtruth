"""Evaluation Report Generator — produces comparison tables with CI.

Reads matrix results and generates:
- Per-benchmark uplift tables
- Per-model comparison
- Confidence intervals from repeated runs
- Accept/reject recommendations per feature
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BenchmarkSummary:
    """Summary statistics for one benchmark/model combination."""

    benchmark: str
    model: str
    mean_resolved_rate: float
    std_resolved_rate: float
    ci_lower: float
    ci_upper: float
    n_runs: int
    uplift_vs_baseline: float | None = None


def load_results(path: str) -> dict:
    """Load matrix results from JSON."""
    return json.loads(Path(path).read_text())


def compute_summary(results: dict) -> list[BenchmarkSummary]:
    """Compute per-benchmark/model statistics."""
    # Group runs by (benchmark, model)
    groups: dict[tuple[str, str], list[float]] = {}
    for run in results.get("runs", []):
        key = (run["benchmark"], run["model"])
        groups.setdefault(key, []).append(run["resolved_rate"])

    summaries: list[BenchmarkSummary] = []
    for (bench, model), rates in groups.items():
        n = len(rates)
        mean = statistics.mean(rates) if rates else 0.0
        std = statistics.stdev(rates) if n > 1 else 0.0

        # 95% CI using t-distribution approximation
        t_value = 2.0  # Approximate for small n
        ci_half = t_value * std / math.sqrt(n) if n > 0 else 0.0

        summaries.append(BenchmarkSummary(
            benchmark=bench,
            model=model,
            mean_resolved_rate=mean,
            std_resolved_rate=std,
            ci_lower=mean - ci_half,
            ci_upper=mean + ci_half,
            n_runs=n,
        ))

    return summaries


def check_acceptance(
    summaries: list[BenchmarkSummary],
    baseline_summaries: list[BenchmarkSummary],
    config: dict,
) -> dict[str, bool]:
    """Check if uplift meets acceptance criteria.

    Returns dict of benchmark → accepted (True/False).
    """
    rules = config.get("acceptance_rules", {})
    min_uplift = rules.get("min_uplift_absolute", 5) / 100.0
    min_uplift_ci = rules.get("min_uplift_with_ci", 3) / 100.0
    min_models = rules.get("min_model_baselines", 2)

    # Build baseline lookup
    baseline_lookup: dict[tuple[str, str], float] = {}
    for s in baseline_summaries:
        baseline_lookup[(s.benchmark, s.model)] = s.mean_resolved_rate

    # Check each benchmark
    results: dict[str, bool] = {}
    for bench in set(s.benchmark for s in summaries):
        bench_summaries = [s for s in summaries if s.benchmark == bench]
        models_passing = 0

        for s in bench_summaries:
            baseline = baseline_lookup.get((s.benchmark, s.model), 0.0)
            uplift = s.mean_resolved_rate - baseline

            # Rule 1: absolute uplift ≥ threshold
            if uplift >= min_uplift:
                models_passing += 1
            # Rule 2: CI excludes +2% variance band
            elif uplift >= min_uplift_ci and s.ci_lower > baseline + 0.02:
                models_passing += 1

        results[bench] = models_passing >= min_models

    return results


def generate_report(
    results_path: str,
    baseline_path: str | None = None,
    config_path: str = "benchmarks/eval_matrix/config.yaml",
) -> str:
    """Generate a markdown evaluation report."""
    import yaml

    results = load_results(results_path)
    summaries = compute_summary(results)

    config = yaml.safe_load(Path(config_path).read_text()) if Path(config_path).exists() else {}

    lines = ["# GT vNext Evaluation Report", ""]

    # Summary table
    lines.append("## Results")
    lines.append("")
    lines.append("| Benchmark | Model | Mean Rate | Std | 95% CI | Runs |")
    lines.append("|-----------|-------|-----------|-----|--------|------|")
    for s in summaries:
        lines.append(
            f"| {s.benchmark} | {s.model} | {s.mean_resolved_rate:.1%} | "
            f"±{s.std_resolved_rate:.1%} | [{s.ci_lower:.1%}, {s.ci_upper:.1%}] | {s.n_runs} |"
        )

    # Acceptance check
    if baseline_path:
        baseline_results = load_results(baseline_path)
        baseline_summaries = compute_summary(baseline_results)
        acceptance = check_acceptance(summaries, baseline_summaries, config)

        lines.append("")
        lines.append("## Acceptance")
        lines.append("")
        for bench, accepted in acceptance.items():
            status = "PASS" if accepted else "FAIL"
            lines.append(f"- {bench}: **{status}**")

    return "\n".join(lines)
