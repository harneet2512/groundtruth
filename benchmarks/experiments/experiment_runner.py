#!/usr/bin/env python3
"""Experiment runner: 3 configs (baseline, standard, adaptive) against hallucination cases.

Reuses the existing 100 hallucination cases and fixture data. No synthetic tasks.
Designed to accept real data (SWE-bench, agent sessions) when available.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Add project root to path so we can import groundtruth
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "benchmarks"))

from groundtruth.ai.briefing import BriefingEngine  # noqa: E402
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing  # noqa: E402
from groundtruth.analysis.risk_scorer import RiskScorer  # noqa: E402
from groundtruth.index.store import SymbolStore  # noqa: E402
from groundtruth.utils.result import Ok  # noqa: E402
from groundtruth.validators.orchestrator import ValidationOrchestrator  # noqa: E402

from _fixtures import LANG_CONFIG, populate_store  # noqa: E402
from experiments.models import (  # noqa: E402
    ExperimentConfig,
    ExperimentReport,
    ExperimentResult,
    ExperimentTask,
)
from runner import BenchmarkCase, load_cases  # noqa: E402


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def cases_to_tasks(cases: list[BenchmarkCase]) -> list[ExperimentTask]:
    """Convert BenchmarkCase list to ExperimentTask list.

    Skips cases missing intent, correct_symbol, or correct_import.
    """
    tasks: list[ExperimentTask] = []
    for c in cases:
        if not c.intent or not c.correct_symbol or not c.correct_import:
            continue
        tasks.append(ExperimentTask(
            case_id=c.id,
            category=c.category,
            language=c.language,
            code=c.code,
            file_path=c.file_path,
            intent=c.intent,
            correct_symbol=c.correct_symbol,
            correct_import=c.correct_import,
        ))
    return tasks


# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------


def setup_language_env(
    lang: str,
) -> tuple[SymbolStore, ValidationOrchestrator, BriefingEngine, AdaptiveBriefing, RiskScorer]:
    """Create in-memory store + populate + return all components."""
    config = LANG_CONFIG[lang]
    store = SymbolStore(":memory:")
    store.initialize()
    populate_store(store, config)

    orchestrator = ValidationOrchestrator(store)
    briefing_engine = BriefingEngine(store, api_key=None)
    risk_scorer = RiskScorer(store)
    adaptive = AdaptiveBriefing(store, risk_scorer)

    return store, orchestrator, briefing_engine, adaptive, risk_scorer


# ---------------------------------------------------------------------------
# Task runners
# ---------------------------------------------------------------------------


async def _check_validation(
    orchestrator: ValidationOrchestrator,
    task: ExperimentTask,
) -> tuple[bool, bool, bool]:
    """Run validation, return (error_detected, fix_suggested, fix_correct)."""
    result = await orchestrator.validate(task.code, task.file_path, task.language)
    if not isinstance(result, Ok):
        return False, False, False

    vr = result.value
    error_detected = len(vr.errors) > 0
    fix_suggested = any(e.get("suggestion") is not None for e in vr.errors)

    fix_correct = False
    if fix_suggested:
        for err in vr.errors:
            suggestion = err.get("suggestion")
            if not suggestion:
                continue
            fix_text = suggestion.get("fix", "")
            if task.correct_symbol and task.correct_symbol in fix_text:
                fix_correct = True
            if task.correct_import and task.correct_import in fix_text:
                fix_correct = True

    return error_detected, fix_suggested, fix_correct


def _compute_risk(
    risk_scorer: RiskScorer, file_path: str
) -> tuple[float, dict[str, float]]:
    """Compute risk score for a file."""
    result = risk_scorer.score_file(file_path)
    if isinstance(result, Ok):
        return result.value.overall_risk, dict(result.value.factors)
    return 0.0, {}


async def run_task_baseline(
    orchestrator: ValidationOrchestrator,
    risk_scorer: RiskScorer,
    task: ExperimentTask,
) -> ExperimentResult:
    """Baseline: validate only, no briefing."""
    start = time.monotonic()

    detected, suggested, correct = await _check_validation(orchestrator, task)
    risk_score, risk_factors = _compute_risk(risk_scorer, task.file_path)

    elapsed = (time.monotonic() - start) * 1000
    return ExperimentResult(
        case_id=task.case_id,
        config=ExperimentConfig.BASELINE.value,
        category=task.category,
        language=task.language,
        error_detected=detected,
        fix_suggested=suggested,
        fix_correct=correct,
        file_risk_score=risk_score,
        risk_factors=risk_factors,
        latency_ms=elapsed,
    )


async def run_task_standard(
    orchestrator: ValidationOrchestrator,
    briefing_engine: BriefingEngine,
    risk_scorer: RiskScorer,
    task: ExperimentTask,
) -> ExperimentResult:
    """Standard: briefing (FTS5 fallback) + validate."""
    start = time.monotonic()

    # Generate briefing
    briefing_result = await briefing_engine.generate_briefing(
        task.intent, task.file_path
    )
    covers_symbol = False
    covers_import = False
    symbol_count = 0
    compliance = 0.0

    if isinstance(briefing_result, Ok):
        br = briefing_result.value
        symbol_count = len(br.relevant_symbols)
        sym_names = {s.get("name", "") for s in br.relevant_symbols}
        sym_files = {s.get("file", "") for s in br.relevant_symbols}

        covers_symbol = task.correct_symbol in sym_names
        covers_import = any(task.correct_import in f for f in sym_files)

        # Compliance proxy: fraction of correct info covered
        hits = int(covers_symbol) + int(covers_import)
        compliance = hits / 2.0

    # Validate
    detected, suggested, correct = await _check_validation(orchestrator, task)
    risk_score, risk_factors = _compute_risk(risk_scorer, task.file_path)

    elapsed = (time.monotonic() - start) * 1000
    return ExperimentResult(
        case_id=task.case_id,
        config=ExperimentConfig.STANDARD.value,
        category=task.category,
        language=task.language,
        error_detected=detected,
        fix_suggested=suggested,
        fix_correct=correct,
        briefing_covers_correct_symbol=covers_symbol,
        briefing_covers_correct_import=covers_import,
        briefing_symbol_count=symbol_count,
        compliance_proxy=compliance,
        file_risk_score=risk_score,
        risk_factors=risk_factors,
        latency_ms=elapsed,
    )


async def run_task_adaptive(
    orchestrator: ValidationOrchestrator,
    briefing_engine: BriefingEngine,
    adaptive: AdaptiveBriefing,
    risk_scorer: RiskScorer,
    task: ExperimentTask,
) -> ExperimentResult:
    """Adaptive: enhanced briefing + validate."""
    start = time.monotonic()

    # Generate base briefing
    briefing_result = await briefing_engine.generate_briefing(
        task.intent, task.file_path
    )
    covers_symbol = False
    covers_import = False
    symbol_count = 0
    compliance = 0.0

    if isinstance(briefing_result, Ok):
        base = briefing_result.value
        # Enhance with adaptive briefing
        enhanced_result = adaptive.enhance_briefing(base, task.file_path)
        br = enhanced_result.value if isinstance(enhanced_result, Ok) else base

        symbol_count = len(br.relevant_symbols)
        sym_names = {s.get("name", "") for s in br.relevant_symbols}
        sym_files = {s.get("file", "") for s in br.relevant_symbols}

        covers_symbol = task.correct_symbol in sym_names
        covers_import = any(task.correct_import in f for f in sym_files)

        # Check if enhanced briefing text mentions correct symbol/import
        if not covers_symbol and task.correct_symbol in br.briefing:
            covers_symbol = True
        if not covers_import and task.correct_import in br.briefing:
            covers_import = True

        hits = int(covers_symbol) + int(covers_import)
        compliance = hits / 2.0

    # Validate
    detected, suggested, correct = await _check_validation(orchestrator, task)
    risk_score, risk_factors = _compute_risk(risk_scorer, task.file_path)

    elapsed = (time.monotonic() - start) * 1000
    return ExperimentResult(
        case_id=task.case_id,
        config=ExperimentConfig.ADAPTIVE.value,
        category=task.category,
        language=task.language,
        error_detected=detected,
        fix_suggested=suggested,
        fix_correct=correct,
        briefing_covers_correct_symbol=covers_symbol,
        briefing_covers_correct_import=covers_import,
        briefing_symbol_count=symbol_count,
        compliance_proxy=compliance,
        file_risk_score=risk_score,
        risk_factors=risk_factors,
        latency_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_results(
    config: str, results: list[ExperimentResult], elapsed_s: float
) -> ExperimentReport:
    """Compute aggregate rates from a list of results."""
    total = len(results)
    if total == 0:
        return ExperimentReport(config=config, elapsed_s=elapsed_s)

    detected = sum(1 for r in results if r.error_detected)
    fixed = sum(1 for r in results if r.fix_correct)
    mean_compliance = sum(r.compliance_proxy for r in results) / total
    mean_risk = sum(r.file_risk_score for r in results) / total

    # By category
    by_cat: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.category not in by_cat:
            by_cat[r.category] = {"total": 0, "detected": 0, "fix_correct": 0,
                                  "compliance_sum": 0.0}
        by_cat[r.category]["total"] += 1
        if r.error_detected:
            by_cat[r.category]["detected"] += 1
        if r.fix_correct:
            by_cat[r.category]["fix_correct"] += 1
        by_cat[r.category]["compliance_sum"] += r.compliance_proxy

    for cat_data in by_cat.values():
        t = cat_data["total"]
        cat_data["detection_rate"] = cat_data["detected"] / t if t else 0
        cat_data["fix_rate"] = cat_data["fix_correct"] / t if t else 0
        cat_data["mean_compliance"] = cat_data["compliance_sum"] / t if t else 0
        del cat_data["compliance_sum"]

    # By language
    by_lang: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.language not in by_lang:
            by_lang[r.language] = {"total": 0, "detected": 0, "fix_correct": 0,
                                   "compliance_sum": 0.0}
        by_lang[r.language]["total"] += 1
        if r.error_detected:
            by_lang[r.language]["detected"] += 1
        if r.fix_correct:
            by_lang[r.language]["fix_correct"] += 1
        by_lang[r.language]["compliance_sum"] += r.compliance_proxy

    for lang_data in by_lang.values():
        t = lang_data["total"]
        lang_data["detection_rate"] = lang_data["detected"] / t if t else 0
        lang_data["fix_rate"] = lang_data["fix_correct"] / t if t else 0
        lang_data["mean_compliance"] = lang_data["compliance_sum"] / t if t else 0
        del lang_data["compliance_sum"]

    return ExperimentReport(
        config=config,
        total_tasks=total,
        detection_rate=detected / total,
        fix_rate=fixed / total,
        mean_compliance_proxy=mean_compliance,
        mean_risk_score=mean_risk,
        by_category=by_cat,
        by_language=by_lang,
        results=results,
        elapsed_s=elapsed_s,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _result_to_dict(r: ExperimentResult) -> dict[str, Any]:
    return {
        "case_id": r.case_id,
        "config": r.config,
        "category": r.category,
        "language": r.language,
        "error_detected": r.error_detected,
        "fix_suggested": r.fix_suggested,
        "fix_correct": r.fix_correct,
        "briefing_covers_correct_symbol": r.briefing_covers_correct_symbol,
        "briefing_covers_correct_import": r.briefing_covers_correct_import,
        "briefing_symbol_count": r.briefing_symbol_count,
        "compliance_proxy": r.compliance_proxy,
        "file_risk_score": round(r.file_risk_score, 4),
        "risk_factors": {k: round(v, 4) for k, v in r.risk_factors.items()},
        "latency_ms": round(r.latency_ms, 2),
    }


def _report_to_dict(report: ExperimentReport) -> dict[str, Any]:
    return {
        "config": report.config,
        "total_tasks": report.total_tasks,
        "detection_rate": round(report.detection_rate, 4),
        "fix_rate": round(report.fix_rate, 4),
        "mean_compliance_proxy": round(report.mean_compliance_proxy, 4),
        "mean_risk_score": round(report.mean_risk_score, 4),
        "by_category": report.by_category,
        "by_language": report.by_language,
        "elapsed_s": round(report.elapsed_s, 3),
        "results": [_result_to_dict(r) for r in report.results],
    }


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def generate_report_markdown(reports: list[ExperimentReport]) -> str:
    """Generate a human-readable markdown report."""
    lines: list[str] = ["# Experiment Results", ""]

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Config | Tasks | Detection | Fix Rate | Mean Compliance | Elapsed |")
    lines.append("|--------|-------|-----------|----------|-----------------|---------|")
    for r in reports:
        lines.append(
            f"| {r.config} | {r.total_tasks} "
            f"| {_pct(r.detection_rate)} "
            f"| {_pct(r.fix_rate)} "
            f"| {_pct(r.mean_compliance_proxy)} "
            f"| {r.elapsed_s:.2f}s |"
        )
    lines.append("")

    # Per-config breakdowns
    for report in reports:
        lines.append(f"## {report.config.title()}")
        lines.append("")

        if report.by_category:
            lines.append("### By Category")
            lines.append("")
            lines.append("| Category | Total | Detection | Fix Rate | Compliance |")
            lines.append("|----------|-------|-----------|----------|------------|")
            for cat, data in sorted(report.by_category.items()):
                lines.append(
                    f"| {cat} | {data['total']} "
                    f"| {_pct(data['detection_rate'])} "
                    f"| {_pct(data['fix_rate'])} "
                    f"| {_pct(data['mean_compliance'])} |"
                )
            lines.append("")

        if report.by_language:
            lines.append("### By Language")
            lines.append("")
            lines.append("| Language | Total | Detection | Fix Rate | Compliance |")
            lines.append("|----------|-------|-----------|----------|------------|")
            for lang, data in sorted(report.by_language.items()):
                lines.append(
                    f"| {lang} | {data['total']} "
                    f"| {_pct(data['detection_rate'])} "
                    f"| {_pct(data['fix_rate'])} "
                    f"| {_pct(data['mean_compliance'])} |"
                )
            lines.append("")

    return "\n".join(lines)


def write_results(
    reports: list[ExperimentReport], output_dir: Path
) -> None:
    """Write JSON + markdown results to output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON per config
    for report in reports:
        json_path = output_dir / f"{report.config}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(_report_to_dict(report), f, indent=2)

    # Combined markdown
    md_path = output_dir / "experiment_results.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_report_markdown(reports))

    print(f"Results written to {output_dir}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_experiments(
    configs: list[ExperimentConfig],
    language_filter: str = "all",
    max_tasks: int | None = None,
) -> list[ExperimentReport]:
    """Run experiments for the given configs."""
    bench_dir = Path(__file__).resolve().parent.parent
    hallucination_dir = str(bench_dir / "hallucination-cases")

    all_cases = load_cases(hallucination_dir)
    all_tasks = cases_to_tasks(all_cases)

    # Filter by language
    if language_filter != "all":
        all_tasks = [t for t in all_tasks if t.language == language_filter]

    # Determine languages in tasks
    languages = sorted({t.language for t in all_tasks})

    print(f"Experiment Runner — {len(all_tasks)} tasks, {len(configs)} configs")
    print(f"Languages: {', '.join(languages)}\n")

    reports: list[ExperimentReport] = []

    for config in configs:
        print(f"Running config: {config.value}")
        config_start = time.monotonic()
        config_results: list[ExperimentResult] = []

        for lang in languages:
            lang_tasks = [t for t in all_tasks if t.language == lang]
            if max_tasks is not None:
                lang_tasks = lang_tasks[:max_tasks]

            if not lang_tasks:
                continue

            store, orchestrator, briefing_engine, adaptive, risk_scorer = (
                setup_language_env(lang)
            )

            print(f"  [{lang}] {len(lang_tasks)} tasks")

            for task in lang_tasks:
                if config == ExperimentConfig.BASELINE:
                    result = run_task_baseline(orchestrator, risk_scorer, task)
                elif config == ExperimentConfig.STANDARD:
                    result = await run_task_standard(
                        orchestrator, briefing_engine, risk_scorer, task
                    )
                elif config == ExperimentConfig.ADAPTIVE:
                    result = await run_task_adaptive(
                        orchestrator, briefing_engine, adaptive, risk_scorer, task
                    )
                else:
                    continue
                config_results.append(result)

        elapsed = time.monotonic() - config_start
        report = aggregate_results(config.value, config_results, elapsed)
        reports.append(report)
        print(f"  -> {report.total_tasks} tasks, detection={_pct(report.detection_rate)}, "
              f"fix={_pct(report.fix_rate)}, compliance={_pct(report.mean_compliance_proxy)}\n")

    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description="GroundTruth Experiment Runner")
    parser.add_argument(
        "--config",
        default="all",
        choices=["all", "baseline", "standard", "adaptive"],
        help="Which config(s) to run (default: all)",
    )
    parser.add_argument(
        "--language",
        default="all",
        choices=["all", "typescript", "python", "go"],
        help="Filter by language (default: all)",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=None,
        help="Max tasks per language (default: all)",
    )
    args = parser.parse_args()

    if args.config == "all":
        configs = list(ExperimentConfig)
    else:
        configs = [ExperimentConfig(args.config)]

    reports = asyncio.run(run_experiments(configs, args.language, args.tasks))

    output_dir = Path(__file__).resolve().parent / "results"
    write_results(reports, output_dir)

    # Print summary
    print(generate_report_markdown(reports))


if __name__ == "__main__":
    main()
