"""No-MCP benchmark path: run same evaluation in-process (no MCP server)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Project root, src, and benchmarks on path (runner imports _fixtures from benchmarks/)
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(_ROOT / "benchmarks"))

from benchmarks.ab.models import ABReport, MCPProof, RunMetadata
from benchmarks.runner import (
    aggregate,
    evaluate_case,
    evaluate_file_relevance,
    load_cases,
    load_file_relevance_cases,
)
from benchmarks._fixtures import LANG_CONFIG, populate_store  # noqa: I001
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.validators.orchestrator import ValidationOrchestrator


async def run_no_mcp(
    fixture_filter: str = "all",
    run_id: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ABReport:
    """Run the benchmark in-process with no MCP server (same logic as runner.py)."""
    bench_dir = _ROOT / "benchmarks"
    hallucination_dir = str(bench_dir / "hallucination-cases")
    relevance_dir = str(bench_dir / "file-relevance-cases")

    start_time = time.monotonic()

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
        lang = lang_map.get(fixture_filter, fixture_filter)
        languages = [lang] if lang in LANG_CONFIG else list(LANG_CONFIG.keys())

    all_hallucination_cases = load_cases(hallucination_dir)
    all_relevance_cases = load_file_relevance_cases(relevance_dir)

    all_case_results = []
    all_file_relevance_results = []

    for lang in languages:
        config = LANG_CONFIG[lang]
        store = SymbolStore(":memory:")
        store.initialize()
        populate_store(store, config)

        graph = ImportGraph(store)
        tracker = InterventionTracker(store)
        orchestrator = ValidationOrchestrator(store)

        lang_cases = [c for c in all_hallucination_cases if c.language == lang]
        lang_relevance = [c for c in all_relevance_cases if c.language == lang]

        for bc in lang_cases:
            result = await evaluate_case(store, orchestrator, bc)
            all_case_results.append(result)

        for fc in lang_relevance:
            result = await evaluate_file_relevance(store, graph, tracker, fc)
            all_file_relevance_results.append(result)

    elapsed_s = time.monotonic() - start_time

    report = aggregate(all_case_results)
    report.file_relevance_results = all_file_relevance_results
    report.elapsed_s = elapsed_s

    metadata = RunMetadata(
        condition="no_mcp",
        mcp_proof=MCPProof(mcp_enabled=False, connection_ok=False, valid=True),
        elapsed_s=elapsed_s,
        total_cases=report.total_cases,
        total_file_relevance=len(all_file_relevance_results),
        run_id=run_id,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    return ABReport(
        metadata=metadata,
        total_cases=report.total_cases,
        detected=report.detected,
        fix_correct=report.fix_correct,
        ai_needed=report.ai_needed,
        briefing_would_inform=report.briefing_would_inform,
        by_category={
            k: {
                "total": v.total,
                "detected": v.detected,
                "fix_correct": v.fix_correct,
                "ai_needed": v.ai_needed,
                "briefing_would_inform": v.briefing_would_inform,
            }
            for k, v in report.by_category.items()
        },
        file_relevance_results=[
            {
                "id": r.id,
                "language": r.language,
                "precision": r.precision,
                "recall": r.recall,
                "missed_files": r.missed_files,
            }
            for r in report.file_relevance_results
        ],
        case_results=[
            {
                "id": r.id,
                "category": r.category,
                "subcategory": r.subcategory,
                "language": r.language,
                "detected": r.detected,
                "fix_correct": r.fix_correct,
                "ai_needed": r.ai_needed,
                "briefing_would_inform": r.briefing_would_inform,
                "latency_ms": r.latency_ms,
            }
            for r in report.case_results
        ],
        elapsed_s=report.elapsed_s,
    )
