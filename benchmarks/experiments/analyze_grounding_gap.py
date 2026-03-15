#!/usr/bin/env python3
"""Analyze grounding gap: standard vs adaptive briefing coverage.

Reads experiment results JSON and computes briefing coverage metrics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load_results(results_dir: Path, config: str) -> list[dict[str, Any]]:
    """Load results for a given config."""
    path = results_dir / f"{config}.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("results", [])


def analyze_grounding_gap(results_dir: Path) -> dict[str, Any]:
    """Compute grounding gap metrics from standard and adaptive results."""
    standard = load_results(results_dir, "standard")
    adaptive = load_results(results_dir, "adaptive")

    analysis: dict[str, Any] = {
        "standard": _compute_coverage(standard),
        "adaptive": _compute_coverage(adaptive),
    }

    # Comparison
    if standard and adaptive:
        std_cov = analysis["standard"]["overall_symbol_coverage"]
        adp_cov = analysis["adaptive"]["overall_symbol_coverage"]
        analysis["symbol_coverage_delta"] = round(adp_cov - std_cov, 4)

        std_comp = analysis["standard"]["mean_compliance"]
        adp_comp = analysis["adaptive"]["mean_compliance"]
        analysis["compliance_delta"] = round(adp_comp - std_comp, 4)

    return analysis


def _compute_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute coverage metrics for a set of results."""
    if not results:
        return {
            "total": 0,
            "overall_symbol_coverage": 0.0,
            "overall_import_coverage": 0.0,
            "mean_compliance": 0.0,
            "by_category": {},
            "by_language": {},
        }

    total = len(results)
    symbol_hits = sum(1 for r in results if r.get("briefing_covers_correct_symbol"))
    import_hits = sum(1 for r in results if r.get("briefing_covers_correct_import"))
    compliance_sum = sum(r.get("compliance_proxy", 0.0) for r in results)

    # By category
    by_cat: dict[str, dict[str, Any]] = {}
    for r in results:
        cat = r.get("category", "unknown")
        if cat not in by_cat:
            by_cat[cat] = {"total": 0, "symbol_hits": 0, "import_hits": 0,
                           "compliance_sum": 0.0}
        by_cat[cat]["total"] += 1
        if r.get("briefing_covers_correct_symbol"):
            by_cat[cat]["symbol_hits"] += 1
        if r.get("briefing_covers_correct_import"):
            by_cat[cat]["import_hits"] += 1
        by_cat[cat]["compliance_sum"] += r.get("compliance_proxy", 0.0)

    for cat_data in by_cat.values():
        t = cat_data["total"]
        cat_data["symbol_coverage"] = round(cat_data["symbol_hits"] / t, 4) if t else 0
        cat_data["import_coverage"] = round(cat_data["import_hits"] / t, 4) if t else 0
        cat_data["mean_compliance"] = round(cat_data["compliance_sum"] / t, 4) if t else 0
        del cat_data["compliance_sum"]

    # By language
    by_lang: dict[str, dict[str, Any]] = {}
    for r in results:
        lang = r.get("language", "unknown")
        if lang not in by_lang:
            by_lang[lang] = {"total": 0, "symbol_hits": 0, "import_hits": 0,
                             "compliance_sum": 0.0}
        by_lang[lang]["total"] += 1
        if r.get("briefing_covers_correct_symbol"):
            by_lang[lang]["symbol_hits"] += 1
        if r.get("briefing_covers_correct_import"):
            by_lang[lang]["import_hits"] += 1
        by_lang[lang]["compliance_sum"] += r.get("compliance_proxy", 0.0)

    for lang_data in by_lang.values():
        t = lang_data["total"]
        lang_data["symbol_coverage"] = round(lang_data["symbol_hits"] / t, 4) if t else 0
        lang_data["import_coverage"] = round(lang_data["import_hits"] / t, 4) if t else 0
        lang_data["mean_compliance"] = round(lang_data["compliance_sum"] / t, 4) if t else 0
        del lang_data["compliance_sum"]

    return {
        "total": total,
        "overall_symbol_coverage": round(symbol_hits / total, 4),
        "overall_import_coverage": round(import_hits / total, 4),
        "mean_compliance": round(compliance_sum / total, 4),
        "by_category": by_cat,
        "by_language": by_lang,
    }


def generate_markdown(analysis: dict[str, Any]) -> str:
    """Generate markdown report from grounding gap analysis."""
    lines: list[str] = ["# Grounding Gap Analysis", ""]

    for config in ["standard", "adaptive"]:
        data = analysis.get(config, {})
        if not data or data.get("total", 0) == 0:
            continue

        lines.append(f"## {config.title()} Briefing")
        lines.append("")
        lines.append(f"- **Tasks:** {data['total']}")
        lines.append(f"- **Symbol coverage:** {data['overall_symbol_coverage']:.1%}")
        lines.append(f"- **Import coverage:** {data['overall_import_coverage']:.1%}")
        lines.append(f"- **Mean compliance proxy:** {data['mean_compliance']:.1%}")
        lines.append("")

        if data.get("by_category"):
            lines.append("### By Category")
            lines.append("")
            lines.append("| Category | Tasks | Symbol Cov | Import Cov | Compliance |")
            lines.append("|----------|-------|------------|------------|------------|")
            for cat, cd in sorted(data["by_category"].items()):
                lines.append(
                    f"| {cat} | {cd['total']} "
                    f"| {cd['symbol_coverage']:.1%} "
                    f"| {cd['import_coverage']:.1%} "
                    f"| {cd['mean_compliance']:.1%} |"
                )
            lines.append("")

        if data.get("by_language"):
            lines.append("### By Language")
            lines.append("")
            lines.append("| Language | Tasks | Symbol Cov | Import Cov | Compliance |")
            lines.append("|----------|-------|------------|------------|------------|")
            for lang, ld in sorted(data["by_language"].items()):
                lines.append(
                    f"| {lang} | {ld['total']} "
                    f"| {ld['symbol_coverage']:.1%} "
                    f"| {ld['import_coverage']:.1%} "
                    f"| {ld['mean_compliance']:.1%} |"
                )
            lines.append("")

    # Comparison
    if "symbol_coverage_delta" in analysis:
        lines.append("## Standard vs Adaptive")
        lines.append("")
        lines.append(
            f"- Symbol coverage delta: {analysis['symbol_coverage_delta']:+.1%}"
        )
        lines.append(
            f"- Compliance delta: {analysis['compliance_delta']:+.1%}"
        )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    results_dir = Path(__file__).resolve().parent / "results"
    if not results_dir.exists():
        print("No results directory found. Run experiment_runner.py first.")
        sys.exit(1)

    analysis = analyze_grounding_gap(results_dir)

    # Write outputs
    with open(results_dir / "grounding_gap.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2)

    md = generate_markdown(analysis)
    with open(results_dir / "grounding_gap.md", "w", encoding="utf-8") as f:
        f.write(md)

    print(md)


if __name__ == "__main__":
    main()
