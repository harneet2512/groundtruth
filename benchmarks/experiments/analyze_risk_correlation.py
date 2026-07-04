#!/usr/bin/env python3
"""Analyze correlation between risk factors and detection rates.

Reads experiment results JSON, buckets by risk factors, computes Pearson
correlation using stdlib statistics.correlation() (Python 3.11+).
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

# The 6 risk factors from RiskScorer
RISK_FACTORS = [
    "naming_ambiguity",
    "import_depth",
    "convention_variance",
    "overloaded_paths",
    "parameter_complexity",
    "isolation_score",
]


def load_all_results(results_dir: Path) -> list[dict[str, Any]]:
    """Load results from all config JSONs, preferring baseline for detection data."""
    for config in ["baseline", "standard", "adaptive"]:
        path = results_dir / f"{config}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            results = data.get("results", [])
            if results:
                return results
    return []


def analyze_risk_correlation(results_dir: Path) -> dict[str, Any]:
    """Compute correlation between risk factors and detection rates."""
    results = load_all_results(results_dir)
    if not results:
        return {"error": "No results found", "factors": {}}

    analysis: dict[str, Any] = {"total_results": len(results), "factors": {}}

    for factor in RISK_FACTORS:
        factor_values: list[float] = []
        detection_values: list[float] = []

        for r in results:
            risk_factors = r.get("risk_factors", {})
            fv = risk_factors.get(factor, 0.0)
            factor_values.append(fv)
            detection_values.append(1.0 if r.get("error_detected") else 0.0)

        factor_data: dict[str, Any] = {
            "mean_value": round(statistics.mean(factor_values), 4) if factor_values else 0,
        }

        # Compute correlation if there's variance in both series
        try:
            if (len(set(factor_values)) > 1 and len(set(detection_values)) > 1
                    and len(factor_values) >= 2):
                corr = statistics.correlation(factor_values, detection_values)
                factor_data["correlation"] = round(corr, 4)
            else:
                factor_data["correlation"] = None
                factor_data["note"] = "Insufficient variance for correlation"
        except (statistics.StatisticsError, ValueError):
            factor_data["correlation"] = None
            factor_data["note"] = "Could not compute correlation"

        # Bucket analysis: low (<0.3), medium (0.3-0.6), high (>0.6)
        buckets: dict[str, dict[str, int]] = {
            "low": {"total": 0, "detected": 0},
            "medium": {"total": 0, "detected": 0},
            "high": {"total": 0, "detected": 0},
        }
        for r in results:
            fv = r.get("risk_factors", {}).get(factor, 0.0)
            if fv < 0.3:
                bucket = "low"
            elif fv <= 0.6:
                bucket = "medium"
            else:
                bucket = "high"
            buckets[bucket]["total"] += 1
            if r.get("error_detected"):
                buckets[bucket]["detected"] += 1

        for bdata in buckets.values():
            t = bdata["total"]
            bdata["detection_rate"] = round(bdata["detected"] / t, 4) if t else 0.0

        factor_data["buckets"] = buckets
        analysis["factors"][factor] = factor_data

    return analysis


def generate_markdown(analysis: dict[str, Any]) -> str:
    """Generate markdown report from risk correlation analysis."""
    lines: list[str] = ["# Risk Factor Correlation Analysis", ""]

    if "error" in analysis:
        lines.append(f"**Error:** {analysis['error']}")
        return "\n".join(lines)

    lines.append(f"**Total results analyzed:** {analysis.get('total_results', 0)}")
    lines.append("")

    # Correlation table
    lines.append("## Correlations")
    lines.append("")
    lines.append("| Risk Factor | Mean Value | Correlation with Detection |")
    lines.append("|-------------|------------|---------------------------|")
    for factor in RISK_FACTORS:
        fd = analysis["factors"].get(factor, {})
        mean = fd.get("mean_value", 0)
        corr = fd.get("correlation")
        corr_str = f"{corr:+.4f}" if corr is not None else "N/A"
        lines.append(f"| {factor} | {mean:.4f} | {corr_str} |")
    lines.append("")

    # Bucket breakdown
    lines.append("## Detection Rate by Risk Bucket")
    lines.append("")
    lines.append("| Risk Factor | Low (<0.3) | Medium (0.3-0.6) | High (>0.6) |")
    lines.append("|-------------|------------|-------------------|-------------|")
    for factor in RISK_FACTORS:
        fd = analysis["factors"].get(factor, {})
        buckets = fd.get("buckets", {})
        low = buckets.get("low", {})
        med = buckets.get("medium", {})
        high = buckets.get("high", {})

        def fmt(b: dict[str, Any]) -> str:
            t = b.get("total", 0)
            if t == 0:
                return "- (0)"
            return f"{b.get('detection_rate', 0):.0%} ({t})"

        lines.append(
            f"| {factor} | {fmt(low)} | {fmt(med)} | {fmt(high)} |"
        )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    results_dir = Path(__file__).resolve().parent / "results"
    if not results_dir.exists():
        print("No results directory found. Run experiment_runner.py first.")
        sys.exit(1)

    analysis = analyze_risk_correlation(results_dir)

    with open(results_dir / "risk_correlation.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2)

    md = generate_markdown(analysis)
    with open(results_dir / "risk_correlation.md", "w", encoding="utf-8") as f:
        f.write(md)

    print(md)


if __name__ == "__main__":
    main()
