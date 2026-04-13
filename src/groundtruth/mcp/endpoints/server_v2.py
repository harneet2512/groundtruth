"""MCP server v2 — 4-tool architecture for OpenHands + Qwen.

Registers exactly 4 tools matching the locked prompt contract:
  - gt_orient:  Early codebase layout discovery
  - gt_lookup:  Symbol definition + callers + sibling patterns
  - gt_impact:  Pre-edit structural judgment + obligations
  - gt_check:   Post-edit structural validation (MANDATORY before submit)

Tool names are short (gt_*) to match the prompt template and minimize
token overhead in tool-use messages.
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore, is_graph_db
from groundtruth.index.symbol_resolution import resolve_unique_symbol_file
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err
from groundtruth.mcp.endpoints.check import handle_check
from groundtruth.mcp.endpoints.impact import handle_impact
from groundtruth.mcp.endpoints.references import handle_references
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.observability.writer import TraceWriter
from groundtruth.utils.logger import get_logger

log = get_logger("mcp.server_v2")


def _resolve_tool_symbol_target(
    store: SymbolStore,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Resolve symbol-driven requests before freshness and handler dispatch.

    Returns:
    - resolved: exact file_path for freshness gating
    - ambiguous: abstain with match list
    - missing: no symbol target available
    """
    symbol_arg = arguments.get("symbol")
    if tool_name not in {"gt_lookup", "gt_impact"} or not symbol_arg:
        return {"status": "missing", "file_path": None, "matches": []}

    result = store.find_symbol_by_name(symbol_arg)
    if isinstance(result, Err):
        return {"status": "error", "file_path": None, "matches": []}
    return resolve_unique_symbol_file(result.value or [], arguments.get("file_path"))


def create_server(root_path: str, db_path: str | None = None) -> Server:
    """Create the 4-tool MCP server for OpenHands + Qwen Live Lite."""
    app = Server("groundtruth")

    resolved_db = db_path or os.path.join(root_path, ".groundtruth", "index.db")

    # --- Core components ---
    store = _open_store(resolved_db)
    graph = ImportGraph(store)

    # --- Freshness Gate (precondition for structural truth) ---
    from groundtruth.mcp.freshness_gate import FreshnessGate
    freshness_gate = FreshnessGate(db_path=resolved_db, root_path=root_path)

    # --- Observability ---
    writer = TraceWriter()
    tracer = EndpointTracer(writer)

    # --- Optional synthesis components ---
    obligation_engine = _try_init_obligations(store, graph)
    contradiction_detector = _try_init_contradictions(store)
    freshness_checker = _try_init_freshness()
    abstention_policy = _try_init_abstention()

    # --- Try to init new substrate components ---
    substrate_reader = _try_init_substrate(resolved_db)

    # --- Tool definitions (4 tools, matching prompt contract) ---
    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="gt_orient",
                description=(
                    "Codebase orientation. Returns top-level structure: "
                    "directories, key modules, entry points, hot symbols, "
                    "and module dependency edges. Call early in exploration."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "focus": {
                            "type": "string",
                            "description": "Optional: focus area (e.g., 'auth', 'api', 'models')",
                        },
                    },
                },
            ),
            Tool(
                name="gt_lookup",
                description=(
                    "Symbol lookup. Returns definition site, callers, importers, "
                    "and sibling patterns for a specific symbol. Use when you need "
                    "to understand how a function/class is used."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Symbol name to look up (e.g., 'getUserById', 'MyClass.method')",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Optional file scope for ambiguous symbols",
                        },
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="gt_impact",
                description=(
                    "Pre-edit impact analysis. Before modifying a symbol, shows "
                    "callers at risk, behavioral obligations, and safe vs unsafe "
                    "modifications. Includes contract-backed constraints."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Symbol name to analyze before editing",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Optional file scope for ambiguous symbols",
                        },
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="gt_check",
                description=(
                    "Post-edit structural check. MANDATORY before submitting. "
                    "Detects: broken callers, removed symbols with references, "
                    "signature arity changes, contract violations. "
                    "Only reports blockers — silence means clean."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "File to check (or omit to check all changed files)",
                        },
                    },
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result: dict[str, Any]

        # --- Freshness gate: check before serving structural evidence ---
        file_arg = arguments.get("file_path")
        resolved_symbol = _resolve_tool_symbol_target(store, name, arguments)
        resolved_symbol_file = resolved_symbol["file_path"]
        if resolved_symbol["status"] == "ambiguous":
                result = {
                    "abstained": True,
                    "reason": f"Ambiguous symbol '{arguments['symbol']}' — multiple matches require explicit file scope",
                    "matches": resolved_symbol["matches"][:5],
                }
                text = json.dumps(result, indent=2, default=str)
                return [TextContent(type="text", text=text)]
        # Symbol-based queries are resolved to a concrete file first, then
        # freshness is checked at file scope. File-based queries use file_arg.
        verdict = freshness_gate.check(file_arg or resolved_symbol_file)

        if verdict.should_suppress and name != "gt_orient":
            # ABSTAIN: graph is stale — do not serve wrong structural assertions
            log.warning(
                "[GT_FRESHNESS] ABSTAIN tool=%s reason=%s",
                name, verdict.reason,
            )
            abstain_result = {
                "abstained": True,
                "reason": verdict.reason,
                "action": "Reindex required. Run gt-index to refresh the graph.",
                "_freshness": {
                    "graph_age_seconds": verdict.graph_age_seconds,
                    "stale_files": verdict.stale_files,
                },
            }
            text = json.dumps(abstain_result, indent=2)
            return [TextContent(type="text", text=text)]

        if name == "gt_orient":
            result = await _handle_orient(
                store=store,
                graph=graph,
                root_path=root_path,
                focus=arguments.get("focus"),
            )
        elif name == "gt_lookup":
            result = await handle_references(
                symbol=arguments["symbol"],
                store=store,
                graph=graph,
                root_path=root_path,
                tracer=tracer,
                file_path=arguments.get("file_path"),
            )
        elif name == "gt_impact":
            result = await handle_impact(
                symbol=arguments["symbol"],
                store=store,
                graph=graph,
                root_path=root_path,
                tracer=tracer,
                file_path=arguments.get("file_path"),
                obligation_engine=obligation_engine,
                freshness_checker=freshness_checker,
                abstention_policy=abstention_policy,
            )
            # Enrich with substrate contracts if available
            if substrate_reader:
                contracts = _get_substrate_contracts(
                    substrate_reader,
                    arguments["symbol"],
                    arguments.get("file_path"),
                )
                if contracts:
                    result["contracts"] = contracts
        elif name == "gt_check":
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
            # Enrich with substrate critique if available
            if substrate_reader and arguments.get("file_path"):
                critique_lines = _get_substrate_critique(
                    substrate_reader, arguments["file_path"]
                )
                if critique_lines:
                    result.setdefault("warnings", []).extend(critique_lines)
        else:
            result = {"error": f"Unknown tool: {name}"}

        # --- Freshness gate: downgrade if slightly stale ---
        if verdict.should_downgrade:
            log.info(
                "[GT_FRESHNESS] DOWNGRADE tool=%s reason=%s",
                name, verdict.reason,
            )
            result["freshness_warning"] = verdict.reason
            if "obligations" in result:
                result["obligations_confidence"] = "downgraded — reindex recommended"

        # Add freshness telemetry to every response
        result["_freshness"] = {
            "fresh": verdict.is_fresh,
            "graph_age_s": round(verdict.graph_age_seconds, 1) if verdict.graph_age_seconds else None,
        }

        # Log tool call for utilization tracking
        log.info("tool_call: %s args=%s result_size=%d fresh=%s", name, arguments, len(str(result)), verdict.is_fresh)

        text = json.dumps(result, indent=2, default=str)
        # Enforce token budget: max ~200 tokens ≈ 800 chars
        if len(text) > 800:
            text = text[:780] + '\n  "truncated": true\n}'

        return [TextContent(type="text", text=text)]

    return app


def _open_store(db_path: str) -> SymbolStore:
    """Open either index.db or graph.db behind a single SymbolStore interface."""
    if is_graph_db(db_path):
        store: SymbolStore = GraphStore(db_path)
    else:
        store = SymbolStore(db_path)

    init_result = store.initialize()
    if isinstance(init_result, Err):
        raise RuntimeError(f"Failed to initialize store for {db_path}: {init_result.error.message}")
    return store


# --- gt_orient handler ---

async def _handle_orient(
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    focus: str | None = None,
) -> dict[str, Any]:
    """Handle gt_orient: codebase structure overview.

    Returns: top directories, hot symbols, entry points, module edges.
    Compact output (~180 tokens max).
    """
    from groundtruth.utils.result import Err

    result: dict[str, Any] = {}

    # Top directories by symbol count
    try:
        from groundtruth.index.graph_store import GraphStore, is_graph_db
        # Check if we're using GraphStore
        if hasattr(store, 'get_top_directories'):
            dirs_result = store.get_top_directories(5)
            if not isinstance(dirs_result, Err):
                result["top_dirs"] = dirs_result.value if hasattr(dirs_result, 'value') else dirs_result
    except (ImportError, AttributeError):
        pass

    # Hot symbols (most-referenced)
    hotspots = store.get_hotspots(5)
    if not isinstance(hotspots, Err):
        symbols = hotspots.value if hasattr(hotspots, 'value') else hotspots
        result["hot_symbols"] = [
            {"name": s.name, "file": s.file_path, "refs": s.usage_count}
            for s in (symbols or [])[:5]
        ]

    # File count
    all_files = store.get_all_files() if hasattr(store, 'get_all_files') else None
    if all_files and not isinstance(all_files, Err):
        files = all_files.value if hasattr(all_files, 'value') else all_files
        result["file_count"] = len(files) if files else 0

    if not result:
        result["status"] = "index empty or unavailable"

    return result


# --- Substrate integration ---

def _try_init_substrate(db_path: str) -> Any | None:
    """Try to init the new substrate GraphStoreReader."""
    try:
        from groundtruth.index.graph_store import GraphStore, is_graph_db

        if not is_graph_db(db_path):
            return None

        from groundtruth.substrate.graph_reader_impl import GraphStoreReader

        gs = GraphStore(db_path)
        init_result = gs.initialize()
        if hasattr(init_result, 'is_err') and init_result.is_err():
            return None
        return GraphStoreReader(gs)
    except (ImportError, Exception) as exc:
        log.debug("Substrate init failed: %s", exc)
        return None


def _get_substrate_contracts(
    reader: Any,
    symbol: str,
    file_path: str | None = None,
) -> list[dict] | None:
    """Get contracts for a symbol via the substrate contract engine."""
    try:
        from groundtruth.contracts.engine import ContractEngine

        node = reader.get_node_by_name(symbol, file_path)
        if not node:
            return None

        engine = ContractEngine(reader)
        contracts = engine.extract_all(node["id"])
        if not contracts:
            return None

        return [
            {"type": c.contract_type, "predicate": c.predicate, "tier": c.tier}
            for c in contracts[:3]  # Max 3 for token budget
        ]
    except (ImportError, Exception):
        return None


def _get_substrate_critique(reader: Any, file_path: str) -> list[str]:
    """Get structural critique for a file via the substrate verification layer."""
    try:
        from groundtruth.verification.critique import compute_critique

        return compute_critique(reader, file_path)
    except (ImportError, Exception):
        return []


# --- Optional component initialization ---

def _try_init_obligations(store: SymbolStore, graph: ImportGraph) -> Any | None:
    try:
        from groundtruth.validators.obligations import ObligationEngine
        return ObligationEngine(store, graph)
    except ImportError:
        log.info("obligations module not available")
        return None


def _try_init_contradictions(store: SymbolStore) -> Any | None:
    try:
        from groundtruth.validators.contradictions import ContradictionDetector
        return ContradictionDetector(store)
    except ImportError:
        log.info("contradictions module not available")
        return None


def _try_init_freshness() -> Any | None:
    try:
        from groundtruth.index.freshness import FreshnessChecker
        return FreshnessChecker()
    except ImportError:
        log.info("freshness module not available")
        return None


def _try_init_abstention() -> Any | None:
    try:
        from groundtruth.policy.abstention import AbstentionPolicy
        return AbstentionPolicy()
    except ImportError:
        log.info("abstention module not available")
        return None
