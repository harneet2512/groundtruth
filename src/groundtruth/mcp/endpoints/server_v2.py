"""MCP server v2 — 6 endpoint architecture.

Original 3 tools:
  - groundtruth_impact: Pre-edit structural judgment
  - groundtruth_check: Post-edit completeness check
  - groundtruth_references: Symbol reference lookup

Decision Interface vNext (3 lifecycle surfaces):
  - groundtruth_task_map: Pre-task localization + repo shape
  - groundtruth_event_brief: Post-edit, only new high-value facts
  - groundtruth_review_patch: Pre-submit deterministic diff review
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.endpoints.check import handle_check
from groundtruth.mcp.endpoints.event_brief import handle_event_brief
from groundtruth.mcp.endpoints.impact import handle_impact
from groundtruth.mcp.endpoints.references import handle_references
from groundtruth.mcp.endpoints.review_patch import handle_review_patch
from groundtruth.mcp.endpoints.task_map import handle_task_map
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.observability.writer import TraceWriter
from groundtruth.utils.logger import get_logger

log = get_logger("mcp.server_v2")


def create_server(root_path: str, db_path: str | None = None) -> Server:
    """Create the 3-endpoint MCP server."""
    app = Server("groundtruth")

    # --- Core components ---
    store = SymbolStore(db_path or os.path.join(root_path, ".groundtruth", "index.db"))
    graph = ImportGraph(store)

    # --- Observability ---
    writer = TraceWriter()
    tracer = EndpointTracer(writer)

    # --- Optional synthesis components (imported if available) ---
    obligation_engine = _try_init_obligations(store, graph)
    contradiction_detector = _try_init_contradictions(store)
    freshness_checker = _try_init_freshness()
    abstention_policy = _try_init_abstention()

    # --- Session-scoped novelty filter for vNext surfaces ---
    novelty_filter = NoveltyFilter()

    # --- Tool definitions ---
    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="groundtruth_impact",
                description=(
                    "Pre-edit structural judgment. Before modifying a symbol, "
                    "shows which callers break, what obligations must change together, "
                    "and what are safe vs unsafe modifications."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Symbol name to analyze (e.g. 'getUserById', 'MyClass.method')",
                        },
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="groundtruth_check",
                description=(
                    "Post-edit structural completeness check. After making edits, "
                    "verifies the patch covers all obligation sites, catches hallucinated "
                    "names, and detects structural contradictions. Call before submitting."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Optional: check specific file instead of git diff",
                        },
                        "proposed_code": {
                            "type": "string",
                            "description": "Optional: code to validate (requires file_path)",
                        },
                    },
                },
            ),
            Tool(
                name="groundtruth_references",
                description=(
                    "Find where a symbol is defined and all its usage sites across "
                    "the codebase. Returns import-resolved, AST-verified references "
                    "grouped by source and test files."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Symbol name to look up",
                        },
                    },
                    "required": ["symbol"],
                },
            ),
            # --- Decision Interface vNext: 3 lifecycle surfaces ---
            Tool(
                name="groundtruth_task_map",
                description=(
                    "Pre-task: localization, repo shape, caller/test constraints "
                    "for symbols mentioned in the issue. Call ONCE at task start."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "issue_text": {
                            "type": "string",
                            "description": "Issue or task description text",
                        },
                        "entry_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: known relevant file paths",
                        },
                    },
                    "required": ["issue_text"],
                },
            ),
            Tool(
                name="groundtruth_event_brief",
                description=(
                    "Post-edit: only new deterministic findings for the just-modified "
                    "file. Silent when nothing to say — no output means no issues."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file that was just edited",
                        },
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="groundtruth_review_patch",
                description=(
                    "Pre-submit: full deterministic diff review. Obligations, "
                    "contradictions, call-site voting, argument affinity, guard "
                    "consistency. Call ONCE before submitting the patch."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Optional: review specific file instead of full diff",
                        },
                    },
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        import json

        result: dict[str, Any]

        if name == "groundtruth_impact":
            result = await handle_impact(
                symbol=arguments["symbol"],
                store=store,
                graph=graph,
                root_path=root_path,
                tracer=tracer,
                obligation_engine=obligation_engine,
                freshness_checker=freshness_checker,
                abstention_policy=abstention_policy,
            )
        elif name == "groundtruth_check":
            result = await handle_check(
                store=store,
                graph=graph,
                root_path=root_path,
                tracer=tracer,
                obligation_engine=obligation_engine,
                contradiction_detector=contradiction_detector,
                file_path=arguments.get("file_path"),
                proposed_code=arguments.get("proposed_code"),
                freshness_checker=freshness_checker,
            )
        elif name == "groundtruth_references":
            result = await handle_references(
                symbol=arguments["symbol"],
                store=store,
                graph=graph,
                root_path=root_path,
                tracer=tracer,
            )
        elif name == "groundtruth_task_map":
            result = await handle_task_map(
                issue_text=arguments["issue_text"],
                store=store,
                graph=graph,
                root_path=root_path,
                novelty_filter=novelty_filter,
                entry_files=arguments.get("entry_files"),
            )
        elif name == "groundtruth_event_brief":
            result = await handle_event_brief(
                file_path=arguments["file_path"],
                store=store,
                graph=graph,
                root_path=root_path,
                novelty_filter=novelty_filter,
            )
        elif name == "groundtruth_review_patch":
            result = await handle_review_patch(
                store=store,
                graph=graph,
                root_path=root_path,
                novelty_filter=novelty_filter,
                file_path=arguments.get("file_path"),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

        text = json.dumps(result, indent=2, default=str)
        return [TextContent(type="text", text=text)]

    return app


# --- Optional component initialization ---


def _try_init_obligations(store: SymbolStore, graph: ImportGraph) -> Any | None:
    """Try to import and init the obligation engine."""
    try:
        from groundtruth.validators.obligations import ObligationEngine

        return ObligationEngine(store, graph)
    except ImportError:
        log.info("obligations module not available")
        return None


def _try_init_contradictions(store: SymbolStore) -> Any | None:
    """Try to import and init the contradiction detector."""
    try:
        from groundtruth.validators.contradictions import ContradictionDetector

        return ContradictionDetector(store)
    except ImportError:
        log.info("contradictions module not available")
        return None


def _try_init_freshness() -> Any | None:
    """Try to import and init the freshness checker."""
    try:
        from groundtruth.index.freshness import FreshnessChecker

        return FreshnessChecker()
    except ImportError:
        log.info("freshness module not available")
        return None


def _try_init_abstention() -> Any | None:
    """Try to import and init the abstention policy."""
    try:
        from groundtruth.policy.abstention import AbstentionPolicy

        return AbstentionPolicy()
    except ImportError:
        log.info("abstention module not available")
        return None
