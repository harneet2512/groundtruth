#!/usr/bin/env python3
"""Analyze adaptive briefing improvement over standard briefing.

Pairs standard vs adaptive results by case_id and computes deltas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load_results(results_dir: Path, config: str) -> dict[str, dict[str, Any]]:
    """Load results keyed by case_id."""
    path = results_dir / f"{config}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {r["case_id"]: r for r in data.get("results", [])}


def analyze_adaptive_improvement(results_dir: Path) -> dict[str, Any]:
    """Compare standard vs adaptive, paired by case_id."""
    standard = load_results(results_dir, "standard")
    adaptive = load_results(results_dir, "adaptive")

    if not standard or not adaptive:
        return {"error": "Need both standard and adaptive results"}

    # Only compare cases present in both
    common_ids = sorted(set(standard.keys()) & set(adaptive.keys()))
    if not common_ids:
        return {"error": "No common cases between standard and adaptive"}

    analysis: dict[str, Any] = {"total_paired": len(common_ids)}

    # Overall deltas
    std_detected = sum(1 for cid in common_ids if standard[cid].get("error_detected"))
    adp_detected = sum(1 for cid in common_ids if adaptive[cid].get("error_detected"))
    std_fix = sum(1 for cid in common_ids if standard[cid].get("fix_correct"))
    adp_fix = sum(1 for cid in common_ids if adaptive[cid].get("fix_correct"))
    std_coverage = sum(1 for cid in common_ids
                       if standard[cid].get("briefing_covers_correct_symbol"))
    adp_coverage = sum(1 for cid in common_ids
                       if adaptive[cid].get("briefing_covers_correct_symbol"))
    std_compliance = sum(standard[cid].get("compliance_proxy", 0.0) for cid in common_ids)
    adp_compliance = sum(adaptive[cid].get("compliance_proxy", 0.0) for cid in common_ids)

    n = len(common_ids)
    analysis["overall"] = {
        "standard_detection_rate": round(std_detected / n, 4),
        "adaptive_detection_rate": round(adp_detected / n, 4),
        "detection_delta": round((adp_detected - std_detected) / n, 4),
        "standard_fix_rate": round(std_fix / n, 4),
        "adaptive_fix_rate": round(adp_fix / n, 4),
        "fix_delta": round((adp_fix - std_fix) / n, 4),
        "standard_symbol_coverage": round(std_coverage / n, 4),
        "adaptive_symbol_coverage": round(adp_coverage / n, 4),
        "coverage_delta": round((adp_coverage - std_coverage) / n, 4),
        "standard_mean_compliance": round(std_compliance / n, 4),
        "adaptive_mean_compliance": round(adp_compliance / n, 4),
        "compliance_delta": round((adp_compliance - std_compliance) / n, 4),
    }

    # By category
    by_cat: dict[str, dict[str, list[str]]] = {}
    for cid in common_ids:
        cat = standard[cid].get("category", "unknown")
        if cat not in by_cat:
            by_cat[cat] = {"ids": []}
        by_cat[cat]["ids"].append(cid)

    analysis["by_category"] = {}
    for cat, cat_data in sorted(by_cat.items()):
        ids = cat_data["ids"]
        cn = len(ids)
        s_det = sum(1 for c in ids if standard[c].get("error_detected"))
        a_det = sum(1 for c in ids if adaptive[c].get("error_detected"))
        s_cov = sum(1 for c in ids if standard[c].get("briefing_covers_correct_symbol"))
        a_cov = sum(1 for c in ids if adaptive[c].get("briefing_covers_correct_symbol"))
        s_comp = sum(standard[c].get("compliance_proxy", 0.0) for c in ids)
        a_comp = sum(adaptive[c].get("compliance_proxy", 0.0) for c in ids)

        analysis["by_category"][cat] = {
            "total": cn,
            "detection_delta": round((a_det - s_det) / cn, 4) if cn else 0,
            "coverage_delta": round((a_cov - s_cov) / cn, 4) if cn else 0,
            "compliance_delta": round((a_comp - s_comp) / cn, 4) if cn else 0,
        }

    # By risk level
    analysis["by_risk_level"] = {}
    for level, low, high in [("low", 0.0, 0.3), ("medium", 0.3, 0.6), ("high", 0.6, 1.01)]:
        level_ids = [
            cid for cid in common_ids
            if low <= standard[cid].get("file_risk_score", 0.0) < high
        ]
        ln = len(level_ids)
        if ln == 0:
            analysis["by_risk_level"][level] = {"total": 0}
            continue

        s_comp = sum(standard[c].get("compliance_proxy", 0.0) for c in level_ids)
        a_comp = sum(adaptive[c].get("compliance_proxy", 0.0) for c in level_ids)
        s_det = sum(1 for c in level_ids if standard[c].get("error_detected"))
        a_det = sum(1 for c in level_ids if adaptive[c].get("error_detected"))

        analysis["by_risk_level"][level] = {
            "total": ln,
            "detection_delta": round((a_det - s_det) / ln, 4),
            "compliance_delta": round((a_comp - s_comp) / ln, 4),
        }

    return analysis


def generate_markdown(analysis: dict[str, Any]) -> str:
    """Generate markdown report from adaptive improvement analysis."""
    lines: list[str] = ["# Adaptive vs Standard Briefing Analysis", ""]

    if "error" in analysis:
        lines.append(f"**Error:** {analysis['error']}")
        return "\n".join(lines)

    lines.append(f"**Paired cases:** {analysis.get('total_paired', 0)}")
    lines.append("")

    # Overall comparison
    overall = analysis.get("overall", {})
    if overall:
        lines.append("## Overall")
        lines.append("")
        lines.append("| Metric | Standard | Adaptive | Delta |")
        lines.append("|--------|----------|----------|-------|")
        for metric, std_key, adp_key, delta_key in [
            ("Detection", "standard_detection_rate", "adaptive_detection_rate", "detection_delta"),
            ("Fix Rate", "standard_fix_rate", "adaptive_fix_rate", "fix_delta"),
            ("Symbol Coverage", "standard_symbol_coverage", "adaptive_symbol_coverage",
             "coverage_delta"),
            ("Compliance", "standard_mean_compliance", "adaptive_mean_compliance",
             "compliance_delta"),
        ]:
            s = overall.get(std_key, 0)
            a = overall.get(adp_key, 0)
            d = overall.get(delta_key, 0)
            lines.append(f"| {metric} | {s:.1%} | {a:.1%} | {d:+.1%} |")
        lines.append("")

    # By category
    by_cat = analysis.get("by_category", {})
    if by_cat:
        lines.append("## By Category")
        lines.append("")
        lines.append("| Category | Cases | Detection Delta | Coverage Delta | Compliance Delta |")
        lines.append("|----------|-------|-----------------|----------------|------------------|")
        for cat, cd in sorted(by_cat.items()):
            lines.append(
                f"| {cat} | {cd['total']} "
                f"| {cd.get('detection_delta', 0):+.1%} "
                f"| {cd.get('coverage_delta', 0):+.1%} "
                f"| {cd.get('compliance_delta', 0):+.1%} |"
            )
        lines.append("")

    # By risk level
    by_risk = analysis.get("by_risk_level", {})
    if by_risk:
        lines.append("## By Risk Level")
        lines.append("")
        lines.append("| Risk Level | Cases | Detection Delta | Compliance Delta |")
        lines.append("|------------|-------|-----------------|------------------|")
        for level in ["low", "medium", "high"]:
            rd = by_risk.get(level, {})
            t = rd.get("total", 0)
            if t == 0:
                lines.append(f"| {level} | 0 | - | - |")
            else:
                lines.append(
                    f"| {level} | {t} "
                    f"| {rd.get('detection_delta', 0):+.1%} "
                    f"| {rd.get('compliance_delta', 0):+.1%} |"
                )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    results_dir = Path(__file__).resolve().parent / "results"
    if not results_dir.exists():
        print("No results directory found. Run experiment_runner.py first.")
        sys.exit(1)

    analysis = analyze_adaptive_improvement(results_dir)

    with open(results_dir / "adaptive_improvement.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2)

    md = generate_markdown(analysis)
    with open(results_dir / "adaptive_improvement.md", "w", encoding="utf-8") as f:
        f.write(md)

    print(md)


if __name__ == "__main__":
    main()
