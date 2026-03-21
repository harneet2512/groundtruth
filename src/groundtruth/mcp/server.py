"""MCP server using stdio transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from groundtruth.ai.briefing import BriefingEngine
from groundtruth.ai.task_parser import TaskParser
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing
from groundtruth.analysis.grounding_gap import GroundingGapAnalyzer
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.core import flags
from groundtruth.core.ablation import AblationConfig
from groundtruth.core.communication import CommunicationPolicy, SessionState
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.manager import LSPManager
from groundtruth.mcp.tools import (
    handle_brief,
    handle_check_patch,
    handle_checkpoint,
    handle_confusions,
    handle_context,
    handle_dead_code,
    handle_do,
    handle_explain,
    handle_find_relevant,
    handle_hotspots,
    handle_impact,
    handle_obligations,
    handle_orient,
    handle_patterns,
    handle_scope,
    handle_status,
    handle_symbols,
    handle_trace,
    handle_unused_packages,
    handle_validate,
)
from groundtruth.mcp.tools.core_tools import (
    handle_consolidated_check,
    handle_consolidated_impact,
    handle_consolidated_orient,
    handle_consolidated_references,
    handle_consolidated_search,
)
from groundtruth.stats.token_tracker import TokenTracker
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err
from groundtruth.validators.autocorrect import AutoCorrector
from groundtruth.validators.orchestrator import ValidationOrchestrator

log = get_logger("mcp.server")


async def _safe_call(tool_name: str, coro: Any) -> dict[str, Any]:
    """Wrap a tool handler so unhandled exceptions return structured errors."""
    try:
        result: dict[str, Any] = await coro
        return result
    except Exception:
        log.error("tool_error", tool=tool_name, exc_info=True)
        return {"error": f"Internal error in {tool_name}"}


def create_server(
    root_path: str,
    db_path: str | None = None,
    lsp_trace_dir: Path | None = None,
) -> FastMCP:
    """Create and configure the MCP server."""
    app = FastMCP(name="groundtruth")

    # Initialize shared state
    resolved_db = db_path or os.path.join(root_path, ".groundtruth", "index.db")
    os.makedirs(os.path.dirname(resolved_db), exist_ok=True)
    store = SymbolStore(resolved_db)
    init_result = store.initialize()
    if isinstance(init_result, Err):
        raise RuntimeError(f"Failed to initialize store: {init_result.error.message}")

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)
    token_tracker = TokenTracker()

    # AI components get api_key=None — agents ARE the AI
    task_parser = TaskParser(store, api_key=None)
    briefing_engine = BriefingEngine(store, api_key=None)
    lsp_manager = LSPManager(root_path, trace_dir=lsp_trace_dir)
    orchestrator = ValidationOrchestrator(store, lsp_manager, api_key=None)
    risk_scorer = RiskScorer(store)
    adaptive = AdaptiveBriefing(store, risk_scorer)
    grounding_analyzer = GroundingGapAnalyzer(store)
    autocorrector = AutoCorrector(store, root_path, benchmark_safe=False, graph=graph)

    # Log ablation config at startup
    ablation_config = AblationConfig.from_env()
    if ablation_config.any_enabled():
        log.info("incubator_config", **ablation_config.describe())

    # Communication state machine (gated by GT_ENABLE_COMMUNICATION)
    comm_policy = CommunicationPolicy()
    comm_state: list[SessionState] = [SessionState()]  # mutable ref in list

    def _finalize(tool_name: str, result: dict) -> str:  # type: ignore[type-arg]
        """Serialize result, track tokens, add footprint."""
        # Communication framing (when enabled)
        if flags.communication_enabled():
            comm_state[0] = comm_policy.record_tool_call(comm_state[0], tool_name)
            framing = comm_policy.get_framing(comm_state[0], tool_name)
            if framing:
                result["_framing"] = framing
        response_text = json.dumps(result)
        call_tokens = token_tracker.track(tool_name, response_text)
        result["_token_footprint"] = token_tracker.get_footprint(tool_name, call_tokens)
        return json.dumps(result)

    @app.tool()
    async def groundtruth_find_relevant(
        description: str,
        entry_points: list[str] | None = None,
        entry_symbols: list[str] | None = None,
        max_files: int = 10,
    ) -> str:
        """Find relevant files for a task. Given a task description, returns ranked files."""
        result = await _safe_call(
            "groundtruth_find_relevant",
            handle_find_relevant(
                description=description,
                store=store,
                graph=graph,
                task_parser=task_parser,
                tracker=tracker,
                entry_points=entry_points,
                entry_symbols=entry_symbols,
                max_files=max_files,
            ),
        )
        return _finalize("groundtruth_find_relevant", result)

    @app.tool()
    async def groundtruth_brief(
        intent: str,
        target_file: str | None = None,
    ) -> str:
        """Proactive briefing before code generation. Tell me what I need to know."""
        result = await _safe_call(
            "groundtruth_brief",
            handle_brief(
                intent=intent,
                briefing_engine=briefing_engine,
                tracker=tracker,
                store=store,
                graph=graph,
                target_file=target_file,
                adaptive=adaptive,
            ),
        )
        return _finalize("groundtruth_brief", result)

    @app.tool()
    async def groundtruth_validate(
        proposed_code: str,
        file_path: str,
        language: str | None = None,
    ) -> str:
        """Validate proposed code against the codebase index."""
        result = await _safe_call(
            "groundtruth_validate",
            handle_validate(
                proposed_code=proposed_code,
                file_path=file_path,
                orchestrator=orchestrator,
                tracker=tracker,
                store=store,
                language=language,
                grounding_analyzer=grounding_analyzer,
                root_path=root_path,
                graph=graph,
            ),
        )
        return _finalize("groundtruth_validate", result)

    @app.tool()
    async def groundtruth_trace(
        symbol: str,
        direction: str = "both",
        max_depth: int = 3,
    ) -> str:
        """Trace a symbol through the codebase. Zero AI. Pure graph."""
        result = await _safe_call(
            "groundtruth_trace",
            handle_trace(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                direction=direction,
                max_depth=max_depth,
            ),
        )
        return _finalize("groundtruth_trace", result)

    @app.tool()
    async def groundtruth_status() -> str:
        """Health check and stats."""
        result = await _safe_call("groundtruth_status", handle_status(store=store, tracker=tracker))
        return _finalize("groundtruth_status", result)

    @app.tool()
    async def groundtruth_dead_code() -> str:
        """Find exported symbols with zero references. Pure SQL. Zero AI."""
        result = await _safe_call(
            "groundtruth_dead_code", handle_dead_code(store=store, tracker=tracker)
        )
        return _finalize("groundtruth_dead_code", result)

    @app.tool()
    async def groundtruth_unused_packages() -> str:
        """Find installed packages that no file imports. Pure SQL. Zero AI."""
        result = await _safe_call(
            "groundtruth_unused_packages", handle_unused_packages(store=store, tracker=tracker)
        )
        return _finalize("groundtruth_unused_packages", result)

    @app.tool()
    async def groundtruth_hotspots(limit: int = 20) -> str:
        """Most referenced symbols in the codebase. Pure SQL. Zero AI."""
        result = await _safe_call(
            "groundtruth_hotspots", handle_hotspots(store=store, tracker=tracker, limit=limit)
        )
        return _finalize("groundtruth_hotspots", result)

    @app.tool()
    async def groundtruth_orient() -> str:
        """Codebase orientation — structure, entry points, risk summary."""
        result = await _safe_call(
            "groundtruth_orient",
            handle_orient(
                store=store,
                graph=graph,
                tracker=tracker,
                risk_scorer=risk_scorer,
                root_path=root_path,
            ),
        )
        return _finalize("groundtruth_orient", result)

    @app.tool()
    async def groundtruth_checkpoint() -> str:
        """Session progress summary with recommendations."""
        result = await _safe_call(
            "groundtruth_checkpoint",
            handle_checkpoint(
                store=store,
                tracker=tracker,
                risk_scorer=risk_scorer,
            ),
        )
        return _finalize("groundtruth_checkpoint", result)

    @app.tool()
    async def groundtruth_symbols(file_path: str) -> str:
        """List all symbols in a file with imports and importers."""
        result = await _safe_call(
            "groundtruth_symbols",
            handle_symbols(
                file_path=file_path,
                store=store,
                tracker=tracker,
                root_path=root_path,
            ),
        )
        return _finalize("groundtruth_symbols", result)

    @app.tool()
    async def groundtruth_context(symbol: str, limit: int = 20) -> str:
        """Show symbol usage context with code snippets."""
        result = await _safe_call(
            "groundtruth_context",
            handle_context(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                limit=limit,
            ),
        )
        return _finalize("groundtruth_context", result)

    @app.tool()
    async def groundtruth_explain(
        symbol: str,
        file_path: str | None = None,
    ) -> str:
        """Deep dive into a symbol — source, callers, callees, side effects, complexity."""
        result = await _safe_call(
            "groundtruth_explain",
            handle_explain(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                file_path=file_path,
            ),
        )
        return _finalize("groundtruth_explain", result)

    @app.tool()
    async def groundtruth_impact(
        symbol: str,
        max_depth: int = 3,
    ) -> str:
        """Assess blast radius of modifying a symbol — callers, break risk, safe/unsafe changes."""
        result = await _safe_call(
            "groundtruth_impact",
            handle_impact(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                max_depth=max_depth,
            ),
        )
        return _finalize("groundtruth_impact", result)

    @app.tool()
    async def groundtruth_patterns(file_path: str) -> str:
        """Detect coding conventions in sibling files of the same directory."""
        result = await _safe_call(
            "groundtruth_patterns",
            handle_patterns(
                file_path=file_path,
                store=store,
                tracker=tracker,
                root_path=root_path,
            ),
        )
        return _finalize("groundtruth_patterns", result)

    @app.tool()
    async def groundtruth_check_patch(diff: str) -> str:
        """Validate a diff against the KB. Corrects hallucinated names in imports, methods, attrs, kwargs, classes."""
        result = await _safe_call(
            "groundtruth_check_patch",
            handle_check_patch(
                diff=diff,
                autocorrector=autocorrector,
                tracker=tracker,
            ),
        )
        return _finalize("groundtruth_check_patch", result)

    @app.tool()
    async def groundtruth_scope(symbol: str) -> str:
        """Files needing changes if a symbol changes. Pure graph."""
        result = await _safe_call(
            "groundtruth_scope",
            handle_scope(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
            ),
        )
        return _finalize("groundtruth_scope", result)

    @app.tool()
    async def groundtruth_confusions(repo: str | None = None) -> str:
        """Known hallucination patterns from the corrections log."""
        result = await _safe_call(
            "groundtruth_confusions",
            handle_confusions(
                store=store,
                tracker=tracker,
                repo=repo,
            ),
        )
        return _finalize("groundtruth_confusions", result)

    @app.tool()
    async def groundtruth_obligations(
        symbol: str | None = None,
        diff: str | None = None,
    ) -> str:
        """What MUST change if a symbol changes. Obligations: constructor symmetry, overrides, callers, shared state."""
        result = await _safe_call(
            "groundtruth_obligations",
            handle_obligations(
                symbol=symbol,
                diff=diff,
                store=store,
                graph=graph,
                tracker=tracker,
            ),
        )
        return _finalize("groundtruth_obligations", result)

    @app.tool()
    async def groundtruth_do(
        query: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        scope: str | None = None,
        depth: str = "standard",
        file_path: str | None = None,
        code: str | None = None,
        symbol: str | None = None,
        operation: str | None = None,
    ) -> str:
        """Single entry point for all GroundTruth operations.

        Two modes:
        - Smart auto: provide ``query`` → pipeline inferred from intent + depth.
        - Explicit steps: provide ``steps`` list → caller owns the pipeline.

        ``query`` and ``steps`` are mutually exclusive.
        ``scope`` filters results to files matching the given path prefix.
        """
        result = await _safe_call(
            "groundtruth_do",
            handle_do(
                query=query,
                store=store,
                graph=graph,
                task_parser=task_parser,
                briefing_engine=briefing_engine,
                orchestrator=orchestrator,
                tracker=tracker,
                risk_scorer=risk_scorer,
                adaptive=adaptive,
                grounding_analyzer=grounding_analyzer,
                root_path=root_path,
                operation=operation,
                file_path=file_path,
                code=code,
                symbol=symbol,
                depth=depth,
                steps=steps,
                scope=scope,
            ),
        )
        return _finalize("groundtruth_do", result)

    # ------------------------------------------------------------------
    # Consolidated tools (5 high-signal tools — additive, not replacing)
    # ------------------------------------------------------------------

    @app.tool()
    async def gt_impact(
        symbol: str,
        max_depth: int = 3,
        file_context: str | None = None,
    ) -> str:
        """What changes if I modify this symbol? Returns obligations, callers, scope, blast radius in one call."""
        result = await _safe_call(
            "gt_impact",
            handle_consolidated_impact(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                max_depth=max_depth,
                file_context=file_context,
            ),
        )
        return _finalize("gt_impact", result)

    @app.tool()
    async def gt_references(
        symbol: str,
        max_results: int = 10,
    ) -> str:
        """Where is this symbol used? Returns definition, all references, canonical edit site."""
        result = await _safe_call(
            "gt_references",
            handle_consolidated_references(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                max_results=max_results,
            ),
        )
        return _finalize("gt_references", result)

    @app.tool()
    async def gt_check(
        diff: str,
        file_path: str | None = None,
    ) -> str:
        """Is my patch complete? Checks corrections, obligations, contradictions, suggests tests."""
        result = await _safe_call(
            "gt_check",
            handle_consolidated_check(
                diff=diff,
                autocorrector=autocorrector,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                file_path=file_path,
            ),
        )
        return _finalize("gt_check", result)

    @app.tool()
    async def gt_orient(
        path: str | None = None,
        depth: str = "overview",
    ) -> str:
        """Understand this codebase. Returns structure, hotspots, conventions, index health."""
        result = await _safe_call(
            "gt_orient",
            handle_consolidated_orient(
                store=store,
                graph=graph,
                tracker=tracker,
                risk_scorer=risk_scorer,
                root_path=root_path,
                path=path,
                depth=depth,
            ),
        )
        return _finalize("gt_orient", result)

    @app.tool()
    async def gt_search(
        query: str,
        max_results: int = 10,
        include_dead_code: bool = False,
    ) -> str:
        """Find symbols matching a query. Returns ranked matches plus hotspots."""
        result = await _safe_call(
            "gt_search",
            handle_consolidated_search(
                query=query,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                max_results=max_results,
                include_dead_code=include_dead_code,
            ),
        )
        return _finalize("gt_search", result)

    return app
