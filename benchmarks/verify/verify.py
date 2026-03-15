#!/usr/bin/env python3
"""Pre-benchmark verification — prove GroundTruth works end-to-end on a real repo.

Runs 10 checks exercising every MCP tool handler against a real codebase.
Costs $0, runs locally, catches bugs before spending SWE-bench API credits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Suppress ResourceWarning from ProactorEventLoop on Windows —
# transports get GC'd after the loop closes, causing spurious warnings.
warnings.filterwarnings("ignore", category=ResourceWarning)

# Suppress "Event loop is closed" / "I/O operation on closed pipe" errors
# from asyncio subprocess transport finalizers on Windows.
_original_unraisablehook = sys.unraisablehook


def _quiet_unraisablehook(unraisable: sys.UnraisableHookArgs) -> None:
    exc = unraisable.exc_value
    if exc is not None:
        msg = str(exc)
        if "Event loop is closed" in msg or "I/O operation on closed pipe" in msg:
            return
    _original_unraisablehook(unraisable)


sys.unraisablehook = _quiet_unraisablehook

# Path setup — same pattern as benchmarks/runner.py
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from groundtruth.ai.briefing import BriefingEngine  # noqa: E402
from groundtruth.ai.task_parser import TaskParser  # noqa: E402
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing  # noqa: E402
from groundtruth.analysis.risk_scorer import RiskScorer  # noqa: E402
from groundtruth.index.graph import ImportGraph  # noqa: E402
from groundtruth.index.indexer import Indexer  # noqa: E402
from groundtruth.index.store import SymbolStore  # noqa: E402
from groundtruth.lsp.manager import LSPManager  # noqa: E402
from groundtruth.mcp.tools import (  # noqa: E402
    handle_brief,
    handle_explain,
    handle_find_relevant,
    handle_impact,
    handle_orient,
    handle_patterns,
    handle_validate,
)
from groundtruth.stats.token_tracker import TokenTracker  # noqa: E402
from groundtruth.stats.tracker import InterventionTracker  # noqa: E402
from groundtruth.utils.result import Err, Ok  # noqa: E402
from groundtruth.validators.orchestrator import ValidationOrchestrator  # noqa: E402

from benchmarks.verify.hallucination_cases import (  # noqa: E402
    generate_dynamic_cases,
    get_static_cases,
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Result of a single verification check."""

    check_number: int
    name: str
    passed: bool
    duration_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class VerifyReport:
    """Full verification report."""

    repo_path: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def total(self) -> int:
        return len(self.checks)


# ---------------------------------------------------------------------------
# Component initialization
# ---------------------------------------------------------------------------


def _init_components(
    repo_path: str, db_path: str
) -> tuple[
    SymbolStore,
    ImportGraph,
    InterventionTracker,
    TokenTracker,
    TaskParser,
    BriefingEngine,
    LSPManager,
    ValidationOrchestrator,
    RiskScorer,
    AdaptiveBriefing,
    Indexer,
]:
    """Initialize all dependencies, following mcp/server.py pattern."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    store = SymbolStore(db_path)
    init_result = store.initialize()
    if isinstance(init_result, Err):
        raise RuntimeError(f"Failed to initialize store: {init_result.error.message}")

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)
    token_tracker = TokenTracker()

    task_parser = TaskParser(store, api_key=None)
    briefing_engine = BriefingEngine(store, api_key=None)
    lsp_manager = LSPManager(repo_path)
    orchestrator = ValidationOrchestrator(store, lsp_manager, api_key=None)
    risk_scorer = RiskScorer(store)
    adaptive = AdaptiveBriefing(store, risk_scorer)
    indexer = Indexer(store, lsp_manager)

    return (
        store, graph, tracker, token_tracker,
        task_parser, briefing_engine, lsp_manager, orchestrator,
        risk_scorer, adaptive, indexer,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


async def check_1_index(
    indexer: Indexer, store: SymbolStore, repo_path: str, timeout: int
) -> CheckResult:
    """Check 1: Index the project."""
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            indexer.index_project(repo_path),
            timeout=float(timeout),
        )
        duration = (time.monotonic() - start) * 1000

        if isinstance(result, Err):
            return CheckResult(
                check_number=1, name="Index", passed=False,
                duration_ms=duration, error=result.error.message,
            )

        symbol_count = result.value
        stats_result = store.get_stats()
        stats: dict[str, object] = {}
        if isinstance(stats_result, Ok):
            stats = stats_result.value

        files_count = stats.get("files_count", 0)
        refs_count = stats.get("refs_count", 0)

        passed = symbol_count > 0 and int(str(files_count)) > 0 and int(str(refs_count)) > 0
        return CheckResult(
            check_number=1, name="Index", passed=passed,
            duration_ms=duration,
            details={
                "symbols": symbol_count,
                "files": files_count,
                "refs": refs_count,
            },
        )
    except asyncio.TimeoutError:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=1, name="Index", passed=False,
            duration_ms=duration, error=f"Timed out after {timeout}s",
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=1, name="Index", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_2_risk_score(risk_scorer: RiskScorer) -> CheckResult:
    """Check 2: Risk scoring."""
    start = time.monotonic()
    try:
        result = risk_scorer.score_codebase(limit=50)
        duration = (time.monotonic() - start) * 1000

        if isinstance(result, Err):
            return CheckResult(
                check_number=2, name="Risk Score", passed=False,
                duration_ms=duration, error=result.error.message,
            )

        scores = result.value
        if not scores:
            return CheckResult(
                check_number=2, name="Risk Score", passed=False,
                duration_ms=duration, error="No risk scores returned",
            )

        # Check scores in [0,1]
        all_in_range = all(0.0 <= s.overall_risk <= 1.0 for s in scores)
        # Check at least 2 distinct risk buckets (low/medium/high)
        buckets = set()
        for s in scores:
            if s.overall_risk < 0.25:
                buckets.add("low")
            elif s.overall_risk < 0.5:
                buckets.add("moderate")
            elif s.overall_risk < 0.7:
                buckets.add("high")
            else:
                buckets.add("critical")

        passed = all_in_range and len(buckets) >= 2
        return CheckResult(
            check_number=2, name="Risk Score", passed=passed,
            duration_ms=duration,
            details={
                "count": len(scores),
                "buckets": sorted(buckets),
                "all_in_range": all_in_range,
            },
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=2, name="Risk Score", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_3_orient(
    store: SymbolStore, graph: ImportGraph, tracker: InterventionTracker,
    risk_scorer: RiskScorer, repo_path: str, token_tracker: TokenTracker,
) -> CheckResult:
    """Check 3: Orient handler."""
    start = time.monotonic()
    try:
        result = await handle_orient(
            store=store, graph=graph, tracker=tracker,
            risk_scorer=risk_scorer, root_path=repo_path,
        )
        duration = (time.monotonic() - start) * 1000
        token_tracker.track("groundtruth_orient", json.dumps(result))

        has_project = "project" in result
        symbols_count = 0
        if has_project:
            symbols_count = result["project"].get("symbols_count", 0)
        has_error = "error" in result

        passed = has_project and symbols_count > 0 and not has_error
        return CheckResult(
            check_number=3, name="Orient", passed=passed,
            duration_ms=duration,
            details={"has_project": has_project, "symbols_count": symbols_count},
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=3, name="Orient", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_4_find_relevant(
    store: SymbolStore, graph: ImportGraph, task_parser: TaskParser,
    tracker: InterventionTracker, hub_symbol: str, token_tracker: TokenTracker,
) -> CheckResult:
    """Check 4: Find relevant files."""
    start = time.monotonic()
    try:
        result = await handle_find_relevant(
            description=f"fix {hub_symbol} error handling",
            store=store, graph=graph, task_parser=task_parser,
            tracker=tracker,
        )
        duration = (time.monotonic() - start) * 1000
        token_tracker.track("groundtruth_find_relevant", json.dumps(result))

        files = result.get("files", [])
        has_guidance = "reasoning_guidance" in result
        has_error = "error" in result

        passed = len(files) >= 1 and has_guidance and not has_error
        return CheckResult(
            check_number=4, name="Find Relevant", passed=passed,
            duration_ms=duration,
            details={
                "files_found": len(files),
                "has_reasoning_guidance": has_guidance,
            },
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=4, name="Find Relevant", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_5_brief(
    briefing_engine: BriefingEngine, tracker: InterventionTracker,
    store: SymbolStore, graph: ImportGraph,
    target_file: str | None, adaptive: AdaptiveBriefing,
    token_tracker: TokenTracker,
) -> CheckResult:
    """Check 5: Briefing."""
    start = time.monotonic()
    try:
        result = await handle_brief(
            intent="understand the main entry points and patterns",
            briefing_engine=briefing_engine, tracker=tracker,
            store=store, graph=graph, target_file=target_file,
            adaptive=adaptive,
        )
        duration = (time.monotonic() - start) * 1000
        token_tracker.track("groundtruth_brief", json.dumps(result))

        has_briefing = bool(result.get("briefing"))
        has_symbols = bool(result.get("relevant_symbols"))
        has_error = "error" in result

        passed = (has_briefing or has_symbols) and not has_error
        return CheckResult(
            check_number=5, name="Brief", passed=passed,
            duration_ms=duration,
            details={"has_briefing": has_briefing, "has_symbols": has_symbols},
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=5, name="Brief", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_6_explain(
    store: SymbolStore, graph: ImportGraph, tracker: InterventionTracker,
    repo_path: str, hub_symbol: str, token_tracker: TokenTracker,
) -> CheckResult:
    """Check 6: Explain a symbol."""
    start = time.monotonic()
    try:
        result = await handle_explain(
            symbol=hub_symbol, store=store, graph=graph,
            tracker=tracker, root_path=repo_path,
        )
        duration = (time.monotonic() - start) * 1000
        token_tracker.track("groundtruth_explain", json.dumps(result))

        symbol_info = result.get("symbol", {})
        has_name = bool(symbol_info.get("name"))
        has_file = bool(symbol_info.get("file"))
        has_source = bool(result.get("source_code"))
        has_error = "error" in result

        passed = has_name and has_file and has_source and not has_error
        return CheckResult(
            check_number=6, name="Explain", passed=passed,
            duration_ms=duration,
            details={
                "has_name": has_name, "has_file": has_file,
                "has_source": has_source,
            },
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=6, name="Explain", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_7_impact(
    store: SymbolStore, graph: ImportGraph, tracker: InterventionTracker,
    repo_path: str, hub_symbol: str, token_tracker: TokenTracker,
) -> CheckResult:
    """Check 7: Impact analysis."""
    start = time.monotonic()
    try:
        result = await handle_impact(
            symbol=hub_symbol, store=store, graph=graph,
            tracker=tracker, root_path=repo_path,
        )
        duration = (time.monotonic() - start) * 1000
        token_tracker.track("groundtruth_impact", json.dumps(result))

        has_summary = "impact_summary" in result
        has_error = "error" in result

        passed = has_summary and not has_error
        return CheckResult(
            check_number=7, name="Impact", passed=passed,
            duration_ms=duration,
            details={"has_impact_summary": has_summary},
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=7, name="Impact", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_8_patterns(
    store: SymbolStore, tracker: InterventionTracker, repo_path: str,
    token_tracker: TokenTracker,
) -> CheckResult:
    """Check 8: Patterns detection."""
    start = time.monotonic()
    try:
        # Find a file with siblings
        files_result = store.get_all_files()
        if isinstance(files_result, Err) or not files_result.value:
            return CheckResult(
                check_number=8, name="Patterns", passed=False,
                duration_ms=(time.monotonic() - start) * 1000,
                error="No files in index",
            )

        target_file = None
        for f in files_result.value:
            siblings_result = store.get_sibling_files(f)
            if isinstance(siblings_result, Ok) and len(siblings_result.value) > 0:
                target_file = f
                break

        if target_file is None:
            # No file with siblings — WARN not FAIL
            return CheckResult(
                check_number=8, name="Patterns", passed=True,
                duration_ms=(time.monotonic() - start) * 1000,
                details={"warning": "No file with siblings found, skipped"},
            )

        result = await handle_patterns(
            file_path=target_file, store=store,
            tracker=tracker, root_path=repo_path,
        )
        duration = (time.monotonic() - start) * 1000
        token_tracker.track("groundtruth_patterns", json.dumps(result))

        has_error = "error" in result
        # 0 patterns is WARN not FAIL
        passed = not has_error
        return CheckResult(
            check_number=8, name="Patterns", passed=passed,
            duration_ms=duration,
            details={
                "patterns_count": len(result.get("patterns", [])),
                "target_file": target_file,
            },
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=8, name="Patterns", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_9_validate(
    store: SymbolStore, orchestrator: ValidationOrchestrator,
    tracker: InterventionTracker, token_tracker: TokenTracker,
) -> CheckResult:
    """Check 9: Validate hallucination cases."""
    start = time.monotonic()
    try:
        static_cases = get_static_cases()
        dynamic_cases = generate_dynamic_cases(store)
        all_cases = static_cases + dynamic_cases

        caught = 0
        case_details: list[dict[str, Any]] = []

        for case in all_cases:
            try:
                result = await handle_validate(
                    proposed_code=case.code,
                    file_path=case.file_path,
                    orchestrator=orchestrator,
                    tracker=tracker,
                    store=store,
                )
                token_tracker.track("groundtruth_validate", json.dumps(result))

                is_invalid = result.get("valid") is False
                has_errors = bool(result.get("errors"))
                detected = is_invalid or has_errors

                if detected:
                    caught += 1

                case_details.append({
                    "id": case.id,
                    "category": case.category,
                    "detected": detected,
                })
            except Exception as exc:
                case_details.append({
                    "id": case.id,
                    "category": case.category,
                    "detected": False,
                    "error": str(exc),
                })

        duration = (time.monotonic() - start) * 1000
        total = len(all_cases)
        # Pass if at least 3 out of 5+ cases caught, or at least 3 out of however many
        threshold = min(3, total)
        passed = caught >= threshold and total >= 3
        return CheckResult(
            check_number=9, name="Validate", passed=passed,
            duration_ms=duration,
            details={
                "total_cases": total,
                "caught": caught,
                "threshold": threshold,
                "cases": case_details,
            },
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=9, name="Validate", passed=False,
            duration_ms=duration, error=str(exc),
        )


async def check_10_token_tracking(token_tracker: TokenTracker) -> CheckResult:
    """Check 10: Token tracking — verify session tracking from checks 3-9."""
    start = time.monotonic()
    try:
        session_total = token_tracker.get_session_total()
        breakdown = token_tracker.get_breakdown()
        duration = (time.monotonic() - start) * 1000

        passed = session_total > 0 and len(breakdown) > 0
        return CheckResult(
            check_number=10, name="Token Tracking", passed=passed,
            duration_ms=duration,
            details={
                "session_total": session_total,
                "breakdown": breakdown,
            },
        )
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return CheckResult(
            check_number=10, name="Token Tracking", passed=False,
            duration_ms=duration, error=str(exc),
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _select_hub_symbol(store: SymbolStore) -> str:
    """Pick the most-referenced symbol for use in checks 4, 6, 7."""
    hotspots = store.get_hotspots(1)
    if isinstance(hotspots, Ok) and hotspots.value:
        return hotspots.value[0].name
    # Fallback: pick any symbol
    names = store.get_all_symbol_names()
    if isinstance(names, Ok) and names.value:
        return names.value[0]
    return "main"


def _select_target_file(store: SymbolStore) -> str | None:
    """Pick a target file for the briefing check."""
    ep = store.get_entry_point_files(1)
    if isinstance(ep, Ok) and ep.value:
        return ep.value[0]
    files = store.get_all_files()
    if isinstance(files, Ok) and files.value:
        return files.value[0]
    return None


async def run_verification(
    repo_path: str,
    output_dir: str = "benchmarks/verify/results",
    checks_filter: str | None = None,
    verbose: bool = False,
    timeout: int = 600,
) -> VerifyReport:
    """Run all 10 verification checks."""
    repo_path = os.path.abspath(repo_path)
    db_path = os.path.join(repo_path, ".groundtruth", "verify_index.db")

    # Parse check filter
    selected: set[int] | None = None
    if checks_filter:
        selected = {int(c.strip()) for c in checks_filter.split(",")}

    def should_run(n: int) -> bool:
        return selected is None or n in selected

    report = VerifyReport(repo_path=repo_path)

    print("GroundTruth Pre-Benchmark Verification")
    print(f"{'=' * 50}")
    print(f"Repo: {repo_path}")
    print()

    # Initialize components
    (
        store, graph, tracker, token_tracker,
        task_parser, briefing_engine, lsp_manager, orchestrator,
        risk_scorer, adaptive, indexer,
    ) = _init_components(repo_path, db_path)

    try:
        # Check 1: Index (must run first — other checks depend on it)
        if should_run(1):
            result = await check_1_index(indexer, store, repo_path, timeout)
            report.checks.append(result)
            _print_check(result, verbose)

            if not result.passed:
                print("\n  Index failed — remaining checks cannot run.")
                # Add skipped results for 2-10
                for n in range(2, 11):
                    if should_run(n):
                        report.checks.append(CheckResult(
                            check_number=n,
                            name=_check_name(n),
                            passed=False,
                            duration_ms=0,
                            error="Skipped — indexing failed",
                        ))
                return report

        # Select hub symbol and target file for subsequent checks
        hub_symbol = _select_hub_symbol(store)
        target_file = _select_target_file(store)

        # Check 2: Risk Score
        if should_run(2):
            result = await check_2_risk_score(risk_scorer)
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 3: Orient
        if should_run(3):
            result = await check_3_orient(
                store, graph, tracker, risk_scorer, repo_path, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 4: Find Relevant
        if should_run(4):
            result = await check_4_find_relevant(
                store, graph, task_parser, tracker, hub_symbol, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 5: Brief
        if should_run(5):
            result = await check_5_brief(
                briefing_engine, tracker, store, graph,
                target_file, adaptive, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 6: Explain
        if should_run(6):
            result = await check_6_explain(
                store, graph, tracker, repo_path, hub_symbol, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 7: Impact
        if should_run(7):
            result = await check_7_impact(
                store, graph, tracker, repo_path, hub_symbol, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 8: Patterns
        if should_run(8):
            result = await check_8_patterns(
                store, tracker, repo_path, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 9: Validate
        if should_run(9):
            result = await check_9_validate(
                store, orchestrator, tracker, token_tracker,
            )
            report.checks.append(result)
            _print_check(result, verbose)

        # Check 10: Token Tracking (must run last — depends on 3-9)
        if should_run(10):
            result = await check_10_token_tracking(token_tracker)
            report.checks.append(result)
            _print_check(result, verbose)

    finally:
        # Force-kill all LSP subprocesses BEFORE the event loop closes.
        # On Windows (ProactorEventLoop), transports GC'd after loop close
        # cause "Event loop is closed" / "I/O operation on closed pipe" errors.
        for client in list(lsp_manager._clients.values()):
            proc = getattr(client, "_process", None)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except (asyncio.TimeoutError, OSError, ProcessLookupError):
                    pass
            # Mark client as closed so shutdown_all() is a no-op
            client._closed = True
            client._process = None
            client._started = False
        await lsp_manager.shutdown_all()
        store.close()
        # Clean up verify index
        if os.path.isfile(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass

    # Print summary
    print()
    print(f"{'=' * 50}")
    print(f"Results: {report.passed}/{report.total} passed, "
          f"{report.failed}/{report.total} failed")

    # Save JSON results
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "verify_results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(_report_to_json(report), f, indent=2)
    print(f"Results saved to {results_path}")

    return report


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_CHECK_NAMES = {
    1: "Index", 2: "Risk Score", 3: "Orient", 4: "Find Relevant",
    5: "Brief", 6: "Explain", 7: "Impact", 8: "Patterns",
    9: "Validate", 10: "Token Tracking",
}


def _check_name(n: int) -> str:
    return _CHECK_NAMES.get(n, f"Check {n}")


def _print_check(result: CheckResult, verbose: bool) -> None:
    """Print a single check result."""
    status = "PASS" if result.passed else "FAIL"
    print(f"  [{status}] {result.check_number:>2}. {result.name}"
          f"  ({result.duration_ms:.0f}ms)")
    if result.error:
        print(f"       Error: {result.error}")
    if verbose and result.details:
        for key, val in result.details.items():
            print(f"       {key}: {val}")


def _report_to_json(report: VerifyReport) -> dict[str, Any]:
    """Convert report to JSON-serializable dict."""
    return {
        "repo_path": report.repo_path,
        "passed": report.passed,
        "failed": report.failed,
        "total": report.total,
        "checks": [
            {
                "check_number": c.check_number,
                "name": c.name,
                "passed": c.passed,
                "duration_ms": round(c.duration_ms, 2),
                "details": c.details,
                "error": c.error,
            }
            for c in report.checks
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="GroundTruth Pre-Benchmark Verification",
    )
    parser.add_argument("--repo", required=True, help="Path to repo to verify against")
    parser.add_argument(
        "--output", "-o", default="benchmarks/verify/results",
        help="Output directory for results",
    )
    parser.add_argument(
        "--checks", default=None,
        help="Run specific checks (e.g. '1,5,9')",
    )
    parser.add_argument("--verbose", action="store_true", help="Print full tool responses")
    parser.add_argument(
        "--timeout", type=int, default=600,
        help="Index timeout in seconds (default: 600)",
    )

    args = parser.parse_args()

    report = asyncio.run(run_verification(
        repo_path=args.repo,
        output_dir=args.output,
        checks_filter=args.checks,
        verbose=args.verbose,
        timeout=args.timeout,
    ))
    sys.exit(0 if report.failed == 0 else 1)


if __name__ == "__main__":
    main()
