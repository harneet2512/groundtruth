#!/usr/bin/env python3
"""GTBench — GroundTruth Hallucination Detection Benchmark.

Loads hallucination cases and file relevance cases, populates an in-memory
SQLite store from fixture data, runs validation/find_relevant on each case,
evaluates results, and outputs a report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Add project root to path so we can import groundtruth
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from groundtruth.ai.task_parser import TaskParser  # noqa: E402
from groundtruth.index.graph import ImportGraph  # noqa: E402
from groundtruth.index.store import SymbolStore  # noqa: E402
from groundtruth.mcp.tools import handle_find_relevant  # noqa: E402
from groundtruth.stats.tracker import InterventionTracker  # noqa: E402
from groundtruth.utils.result import Ok  # noqa: E402
from groundtruth.validators.orchestrator import ValidationOrchestrator  # noqa: E402

from _fixtures import LANG_CONFIG, populate_store  # noqa: E402


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkCase:
    """A single hallucination benchmark case (loaded from JSON)."""

    id: str
    category: str
    language: str
    description: str
    code: str
    file_path: str
    intent: str | None = None
    subcategory: str | None = None
    valid: bool = False
    error_type: str | None = None
    fix_type: str | None = None
    correct_symbol: str | None = None
    correct_import: str | None = None
    should_require_ai: bool = False
    briefing_would_prevent: bool | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BenchmarkCase:
        """Load from camelCase JSON, normalizing to snake_case."""
        inp = data.get("input", {})
        exp = data.get("expected", {})
        return cls(
            id=data["id"],
            category=data["category"],
            language=data.get("language", "typescript"),  # default for old cases
            description=data.get("description", ""),
            code=inp.get("code", ""),
            file_path=inp.get("filePath", ""),
            intent=inp.get("intent"),
            subcategory=data.get("subcategory"),
            valid=exp.get("valid", False),
            error_type=exp.get("errorType"),
            fix_type=exp.get("fixType"),
            correct_symbol=exp.get("correctSymbol"),
            correct_import=exp.get("correctImport"),
            should_require_ai=exp.get("shouldRequireAI", False),
            briefing_would_prevent=exp.get("briefingWouldPrevent"),
        )


@dataclass
class CaseResult:
    """Result of evaluating a single hallucination case."""

    id: str
    category: str
    subcategory: str | None
    language: str
    detected: bool
    fix_correct: bool
    ai_needed: bool
    briefing_would_inform: bool
    latency_ms: float = 0.0


@dataclass
class FileRelevanceCase:
    """A single file relevance benchmark case."""

    id: str
    language: str
    task: str
    entry_symbols: list[str]
    expected_files: list[str]
    should_not_include: list[str]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileRelevanceCase:
        return cls(
            id=data["id"],
            language=data["language"],
            task=data["task"],
            entry_symbols=data["entry_symbols"],
            expected_files=data["expected_files"],
            should_not_include=data.get("should_not_include", []),
        )


@dataclass
class FileRelevanceResult:
    """Result of evaluating a single file relevance case."""

    id: str
    language: str
    precision: float
    recall: float
    false_positives: list[str]
    missed_files: list[str]


@dataclass
class CategoryStats:
    """Aggregated stats for a category."""

    total: int = 0
    detected: int = 0
    fix_correct: int = 0
    ai_needed: int = 0
    briefing_would_inform: int = 0


@dataclass
class BenchmarkReport:
    """Full benchmark results."""

    total_cases: int = 0
    detected: int = 0
    fix_correct: int = 0
    ai_needed: int = 0
    briefing_would_inform: int = 0
    by_category: dict[str, CategoryStats] = field(default_factory=dict)
    file_relevance_results: list[FileRelevanceResult] = field(default_factory=list)
    case_results: list[CaseResult] = field(default_factory=list)
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_cases(directory: str) -> list[BenchmarkCase]:
    """Recursively load all JSON benchmark cases from a directory."""
    cases: list[BenchmarkCase] = []
    if not os.path.isdir(directory):
        return cases
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(root, fname)
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            cases.append(BenchmarkCase.from_json(data))
    return cases


def load_file_relevance_cases(directory: str) -> list[FileRelevanceCase]:
    """Load file relevance cases from a directory."""
    cases: list[FileRelevanceCase] = []
    if not os.path.isdir(directory):
        return cases
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
        cases.append(FileRelevanceCase.from_json(data))
    return cases


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

# Error types that the semantic resolver would handle
_AI_RESOLVABLE_TYPES = {"symbol_not_found", "module_not_found", "invented_symbol", "wrong_module_path"}

# Mapping from benchmark errorType to equivalent validator error_type values.
# The benchmark JSON uses one naming convention; validators use another.
_ERROR_TYPE_ALIASES: dict[str, set[str]] = {
    "symbol_not_found": {"invented_symbol", "wrong_module_path", "symbol_not_found"},
    "package_not_installed": {"missing_package", "package_not_installed"},
    "module_not_found": {"missing_package", "module_not_found", "wrong_module_path"},
    "signature_mismatch": {"wrong_arg_count", "signature_mismatch", "wrong_signature"},
}


def _error_type_matches(actual: str, expected: str) -> bool:
    """Check if an actual error type matches the expected benchmark error type."""
    if actual == expected:
        return True
    valid_types = _ERROR_TYPE_ALIASES.get(expected, set())
    return actual in valid_types


async def evaluate_case(
    store: SymbolStore,
    orchestrator: ValidationOrchestrator,
    bc: BenchmarkCase,
) -> CaseResult:
    """Run validation on a benchmark case and evaluate the result."""
    start = time.monotonic()

    result = await orchestrator.validate(bc.code, bc.file_path, bc.language)
    if not isinstance(result, Ok):
        return CaseResult(
            id=bc.id, category=bc.category, subcategory=bc.subcategory,
            language=bc.language, detected=False, fix_correct=False,
            ai_needed=False, briefing_would_inform=False,
        )

    vr = result.value
    errors = vr.errors

    # detected: at least one error matches the expected errorType (with aliasing)
    detected = any(
        _error_type_matches(e.get("type", ""), bc.error_type or "")
        for e in errors
    ) if bc.error_type else False

    # fix_correct: the suggestion contains the correct symbol or import
    fix_correct = False
    if detected and (bc.correct_symbol or bc.correct_import):
        for err in errors:
            if not _error_type_matches(err.get("type", ""), bc.error_type or ""):
                continue
            suggestion = err.get("suggestion")
            if not suggestion:
                continue
            fix_text = suggestion.get("fix", "")
            if bc.correct_symbol and bc.correct_symbol in fix_text:
                fix_correct = True
            if bc.correct_import and bc.correct_import in fix_text:
                fix_correct = True

    # ai_needed: error detected but no fix attached and type is AI-resolvable
    ai_needed = False
    if detected:
        for err in errors:
            if not _error_type_matches(err.get("type", ""), bc.error_type or ""):
                continue
            if not err.get("suggestion") and err.get("type") in _AI_RESOLVABLE_TYPES:
                ai_needed = True
                break

    # briefing_would_inform: FTS5 search on intent returns the correct symbol
    briefing_would_inform = False
    if bc.intent and bc.correct_symbol:
        fts_result = store.search_symbols_fts(bc.intent)
        if isinstance(fts_result, Ok):
            briefing_would_inform = any(
                s.name == bc.correct_symbol for s in fts_result.value
            )

    elapsed = (time.monotonic() - start) * 1000

    return CaseResult(
        id=bc.id,
        category=bc.category,
        subcategory=bc.subcategory,
        language=bc.language,
        detected=detected,
        fix_correct=fix_correct,
        ai_needed=ai_needed,
        briefing_would_inform=briefing_would_inform,
        latency_ms=elapsed,
    )


async def evaluate_file_relevance(
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    fc: FileRelevanceCase,
) -> FileRelevanceResult:
    """Run find_relevant on a file relevance case and evaluate precision/recall."""
    # Mock task parser to return the given entry_symbols
    task_parser = MagicMock(spec=TaskParser)
    task_parser.parse = AsyncMock(return_value=Ok(fc.entry_symbols))

    result = await handle_find_relevant(
        description=fc.task,
        store=store,
        graph=graph,
        task_parser=task_parser,
        tracker=tracker,
    )

    found_paths = {f["path"] for f in result.get("files", [])}
    expected_set = set(fc.expected_files)
    excluded_set = set(fc.should_not_include)

    # Recall: how many expected files were found
    found_expected = expected_set & found_paths
    recall = len(found_expected) / len(expected_set) if expected_set else 1.0

    # Precision: how many found files were expected (excluding known-bad)
    false_positives = list(found_paths & excluded_set)
    # Precision = expected hits / total found (penalize for including excluded files)
    if found_paths:
        precision = len(found_expected) / len(found_paths)
    else:
        precision = 0.0

    missed = list(expected_set - found_paths)

    return FileRelevanceResult(
        id=fc.id,
        language=fc.language,
        precision=precision,
        recall=recall,
        false_positives=false_positives,
        missed_files=missed,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(results: list[CaseResult]) -> BenchmarkReport:
    """Aggregate case results into a report."""
    report = BenchmarkReport(
        total_cases=len(results),
        case_results=results,
    )

    by_cat: dict[str, CategoryStats] = {}

    for r in results:
        key = f"{r.category}/{r.subcategory}" if r.subcategory else r.category
        if key not in by_cat:
            by_cat[key] = CategoryStats()
        cat = by_cat[key]
        cat.total += 1
        if r.detected:
            report.detected += 1
            cat.detected += 1
        if r.fix_correct:
            report.fix_correct += 1
            cat.fix_correct += 1
        if r.ai_needed:
            report.ai_needed += 1
            cat.ai_needed += 1
        if r.briefing_would_inform:
            report.briefing_would_inform += 1
            cat.briefing_would_inform += 1

    report.by_category = by_cat
    return report


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _pct(n: int, total: int) -> str:
    if total == 0:
        return "N/A"
    return f"{n / total * 100:.1f}%"


def generate_report_text(report: BenchmarkReport) -> str:
    """Generate a human-readable markdown report."""
    lines: list[str] = []
    lines.append("# GTBench Results")
    lines.append("")
    lines.append(f"**Total cases:** {report.total_cases}")
    lines.append(f"**Detection rate:** {report.detected}/{report.total_cases}"
                 f" ({_pct(report.detected, report.total_cases)})")
    lines.append(f"**Fix rate (deterministic):** {report.fix_correct}/{report.total_cases}"
                 f" ({_pct(report.fix_correct, report.total_cases)})")
    lines.append(f"**AI needed:** {report.ai_needed}/{report.total_cases}"
                 f" ({_pct(report.ai_needed, report.total_cases)})")
    lines.append(f"**Briefing would inform:** {report.briefing_would_inform}/{report.total_cases}"
                 f" ({_pct(report.briefing_would_inform, report.total_cases)})")
    lines.append(f"**Elapsed:** {report.elapsed_s:.2f}s")
    lines.append("")

    # Category breakdown
    lines.append("## By Category")
    lines.append("")
    lines.append("| Category | Cases | Detected | Fix OK | AI Needed | Briefing |")
    lines.append("|----------|-------|----------|--------|-----------|----------|")
    for cat_name, cat in sorted(report.by_category.items()):
        lines.append(
            f"| {cat_name} | {cat.total} "
            f"| {cat.detected} ({_pct(cat.detected, cat.total)}) "
            f"| {cat.fix_correct} ({_pct(cat.fix_correct, cat.total)}) "
            f"| {cat.ai_needed} ({_pct(cat.ai_needed, cat.total)}) "
            f"| {cat.briefing_would_inform} ({_pct(cat.briefing_would_inform, cat.total)}) |"
        )
    lines.append("")

    # File relevance
    if report.file_relevance_results:
        lines.append("## File Relevance")
        lines.append("")
        avg_precision = sum(r.precision for r in report.file_relevance_results) / len(
            report.file_relevance_results
        )
        avg_recall = sum(r.recall for r in report.file_relevance_results) / len(
            report.file_relevance_results
        )
        lines.append(f"**Cases:** {len(report.file_relevance_results)}")
        lines.append(f"**Avg precision:** {avg_precision:.1%}")
        lines.append(f"**Avg recall:** {avg_recall:.1%}")
        lines.append("")
        lines.append("| Case | Language | Precision | Recall | Missed |")
        lines.append("|------|----------|-----------|--------|--------|")
        for fr in report.file_relevance_results:
            missed = ", ".join(fr.missed_files) if fr.missed_files else "-"
            lines.append(
                f"| {fr.id} | {fr.language} "
                f"| {fr.precision:.1%} | {fr.recall:.1%} "
                f"| {missed} |"
            )
        lines.append("")

    # Missed cases
    missed_cases = [r for r in report.case_results if not r.detected]
    if missed_cases:
        lines.append("## Missed Cases")
        lines.append("")
        for m in missed_cases:
            sub = f"/{m.subcategory}" if m.subcategory else ""
            lines.append(f"- **{m.id}**: {m.category}{sub} ({m.language})")
        lines.append("")

    return "\n".join(lines)


def generate_report_json(report: BenchmarkReport) -> dict[str, Any]:
    """Generate JSON-serializable report."""
    return {
        "total_cases": report.total_cases,
        "detected": report.detected,
        "fix_correct": report.fix_correct,
        "ai_needed": report.ai_needed,
        "briefing_would_inform": report.briefing_would_inform,
        "detection_rate": report.detected / report.total_cases if report.total_cases else 0,
        "fix_rate": report.fix_correct / report.total_cases if report.total_cases else 0,
        "ai_rate": report.ai_needed / report.total_cases if report.total_cases else 0,
        "briefing_rate": (
            report.briefing_would_inform / report.total_cases if report.total_cases else 0
        ),
        "elapsed_s": report.elapsed_s,
        "by_category": {
            name: {
                "total": cat.total,
                "detected": cat.detected,
                "fix_correct": cat.fix_correct,
                "ai_needed": cat.ai_needed,
                "briefing_would_inform": cat.briefing_would_inform,
            }
            for name, cat in sorted(report.by_category.items())
        },
        "file_relevance": {
            "count": len(report.file_relevance_results),
            "avg_precision": (
                sum(r.precision for r in report.file_relevance_results)
                / len(report.file_relevance_results)
                if report.file_relevance_results
                else 0
            ),
            "avg_recall": (
                sum(r.recall for r in report.file_relevance_results)
                / len(report.file_relevance_results)
                if report.file_relevance_results
                else 0
            ),
        },
        "cases": [
            {
                "id": r.id,
                "category": r.category,
                "subcategory": r.subcategory,
                "language": r.language,
                "detected": r.detected,
                "fix_correct": r.fix_correct,
                "ai_needed": r.ai_needed,
                "briefing_would_inform": r.briefing_would_inform,
                "latency_ms": round(r.latency_ms, 2),
            }
            for r in report.case_results
        ],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_benchmark(fixture_filter: str = "all") -> BenchmarkReport:
    """Run the full benchmark suite."""
    bench_dir = Path(__file__).resolve().parent
    hallucination_dir = str(bench_dir / "hallucination-cases")
    relevance_dir = str(bench_dir / "file-relevance-cases")
    results_dir = bench_dir / "results"
    results_dir.mkdir(exist_ok=True)

    print("GTBench — GroundTruth Hallucination Detection Benchmark\n")

    start_time = time.monotonic()

    # Determine which languages to run
    if fixture_filter == "all":
        languages = list(LANG_CONFIG.keys())
    else:
        lang_map = {
            "project_ts": "typescript",
            "project_py": "python",
            "project_go": "go",
            "typescript": "typescript",
            "python": "python",
            "go": "go",
        }
        lang = lang_map.get(fixture_filter)
        if not lang:
            print(f"Unknown fixture: {fixture_filter}")
            sys.exit(1)
        languages = [lang]

    # Load all cases
    all_hallucination_cases = load_cases(hallucination_dir)
    all_relevance_cases = load_file_relevance_cases(relevance_dir)

    print(f"Loaded {len(all_hallucination_cases)} hallucination cases")
    print(f"Loaded {len(all_relevance_cases)} file relevance cases")
    print(f"Languages: {', '.join(languages)}\n")

    # Run hallucination benchmarks per language
    all_case_results: list[CaseResult] = []
    all_file_relevance_results: list[FileRelevanceResult] = []

    for lang in languages:
        config = LANG_CONFIG[lang]
        store = SymbolStore(":memory:")
        store.initialize()
        populate_store(store, config)

        graph = ImportGraph(store)
        tracker = InterventionTracker(store)
        orchestrator = ValidationOrchestrator(store)

        # Filter cases by language
        lang_cases = [c for c in all_hallucination_cases if c.language == lang]
        lang_relevance = [c for c in all_relevance_cases if c.language == lang]

        print(f"  [{lang}] {len(lang_cases)} hallucination cases, "
              f"{len(lang_relevance)} file relevance cases")

        # Evaluate hallucination cases
        for bc in lang_cases:
            result = await evaluate_case(store, orchestrator, bc)
            all_case_results.append(result)

        # Evaluate file relevance cases
        for fc in lang_relevance:
            result = await evaluate_file_relevance(store, graph, tracker, fc)
            all_file_relevance_results.append(result)

    elapsed_s = time.monotonic() - start_time

    # Aggregate
    report = aggregate(all_case_results)
    report.file_relevance_results = all_file_relevance_results
    report.elapsed_s = elapsed_s

    # Generate reports
    md_report = generate_report_text(report)
    json_report = generate_report_json(report)

    # Output
    print()
    print(md_report)

    # Write files
    with open(results_dir / "latest.md", "w", encoding="utf-8") as f:
        f.write(md_report)
    with open(results_dir / "latest.json", "w", encoding="utf-8") as f:
        json.dump(json_report, f, indent=2)

    print(f"\nResults written to {results_dir / 'latest.md'}")
    print(f"JSON results written to {results_dir / 'latest.json'}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="GTBench — GroundTruth Benchmark Runner")
    parser.add_argument(
        "--fixture",
        default="all",
        choices=["all", "project_ts", "project_py", "project_go",
                 "typescript", "python", "go"],
        help="Which fixture(s) to benchmark (default: all)",
    )
    args = parser.parse_args()
    asyncio.run(run_benchmark(args.fixture))


if __name__ == "__main__":
    main()
