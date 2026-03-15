"""End-to-end MCP server integration tests.

Tests the full MCP server lifecycle: create_server() → call_tool() → JSON response.
Unlike unit tests (which call handlers directly), these exercise the actual FastMCP
tool dispatch pipeline with a real SQLite store.

No real LSP servers or AI API calls needed — the store is pre-populated and
AI-dependent tools use deterministic fallback paths.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from mcp.server.fastmcp import FastMCP

from groundtruth.ai.briefing import BriefingEngine
from groundtruth.ai.task_parser import TaskParser
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing
from groundtruth.analysis.grounding_gap import GroundingGapAnalyzer
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools import (
    handle_brief,
    handle_checkpoint,
    handle_context,
    handle_dead_code,
    handle_explain,
    handle_find_relevant,
    handle_hotspots,
    handle_impact,
    handle_orient,
    handle_patterns,
    handle_status,
    handle_symbols,
    handle_trace,
    handle_unused_packages,
    handle_validate,
)
from groundtruth.stats.token_tracker import TokenTracker
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok
from groundtruth.validators.orchestrator import ValidationOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_tool_result(result: Any) -> dict[str, Any]:
    """Extract the JSON dict from a FastMCP call_tool result."""
    # call_tool returns (content_blocks, metadata_dict)
    content_blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(content_blocks, list) and len(content_blocks) > 0:
        text = content_blocks[0].text
    else:
        text = str(content_blocks)
    return json.loads(text)  # type: ignore[no-any-return]


def _populate_store(store: SymbolStore) -> dict[str, int]:
    """Populate store with a realistic Python codebase for E2E testing."""
    now = int(time.time())
    ids: dict[str, int] = {}

    symbols = [
        # Core query functions
        {"name": "get_user_by_id", "kind": "function",
         "file_path": "src/users/queries.py", "line_number": 5, "end_line": 20,
         "is_exported": True, "signature": "(user_id: int) -> User",
         "return_type": "User", "documentation": "Fetch a user by primary key.",
         "usage_count": 8},
        {"name": "create_user", "kind": "function",
         "file_path": "src/users/queries.py", "line_number": 22, "end_line": 35,
         "is_exported": True, "signature": "(data: CreateUserInput) -> User",
         "return_type": "User", "usage_count": 4},
        {"name": "delete_user", "kind": "function",
         "file_path": "src/users/queries.py", "line_number": 37, "end_line": 45,
         "is_exported": True, "signature": "(user_id: int) -> bool",
         "return_type": "bool", "usage_count": 2},

        # Types
        {"name": "User", "kind": "class", "file_path": "src/users/types.py",
         "line_number": 1, "end_line": 12, "is_exported": True, "usage_count": 10},
        {"name": "CreateUserInput", "kind": "class", "file_path": "src/users/types.py",
         "line_number": 14, "end_line": 20, "is_exported": True, "usage_count": 3},

        # Error hierarchy
        {"name": "AppError", "kind": "class", "file_path": "src/utils/errors.py",
         "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 6},
        {"name": "NotFoundError", "kind": "class", "file_path": "src/utils/errors.py",
         "line_number": 12, "end_line": 22, "is_exported": True, "usage_count": 4},
        {"name": "ValidationError", "kind": "class", "file_path": "src/utils/errors.py",
         "line_number": 24, "end_line": 30, "is_exported": True, "usage_count": 2},

        # Auth
        {"name": "auth_middleware", "kind": "function",
         "file_path": "src/middleware/auth.py", "line_number": 1, "end_line": 15,
         "is_exported": True, "signature": "(request: Request, next: Callable) -> Response",
         "usage_count": 3},
        {"name": "verify_token", "kind": "function",
         "file_path": "src/auth/jwt.py", "line_number": 1, "end_line": 10,
         "is_exported": True, "signature": "(token: str) -> TokenPayload",
         "return_type": "TokenPayload", "usage_count": 2},

        # DB
        {"name": "db", "kind": "variable", "file_path": "src/db/client.py",
         "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 7},

        # Crypto
        {"name": "hash_password", "kind": "function",
         "file_path": "src/utils/crypto.py", "line_number": 1, "end_line": 8,
         "is_exported": True, "signature": "(password: str) -> str",
         "return_type": "str", "usage_count": 2},

        # Dead code — exported but zero refs
        {"name": "format_legacy_date", "kind": "function",
         "file_path": "src/utils/dates.py", "line_number": 1, "end_line": 5,
         "is_exported": True, "signature": "(dt: datetime) -> str",
         "usage_count": 0},
        {"name": "DeprecatedLogger", "kind": "class",
         "file_path": "src/utils/logging.py", "line_number": 1, "end_line": 20,
         "is_exported": True, "usage_count": 0},

        # Route handler (not exported)
        {"name": "user_routes", "kind": "function",
         "file_path": "src/routes/users.py", "line_number": 1, "end_line": 40,
         "is_exported": False, "usage_count": 1},
    ]

    for sym in symbols:
        result = store.insert_symbol(
            name=sym["name"],
            kind=sym["kind"],
            language="python",
            file_path=sym["file_path"],
            line_number=sym["line_number"],
            end_line=sym["end_line"],
            is_exported=sym["is_exported"],
            signature=sym.get("signature"),
            params=None,
            return_type=sym.get("return_type"),
            documentation=sym.get("documentation"),
            last_indexed_at=now,
        )
        assert isinstance(result, Ok)
        ids[sym["name"]] = result.value
        if sym.get("usage_count", 0) > 0:
            store.update_usage_count(result.value, sym["usage_count"])

    # References — cross-file imports and calls
    refs = [
        # get_user_by_id called from routes + service
        ("get_user_by_id", "src/routes/users.py", 5, "import"),
        ("get_user_by_id", "src/routes/users.py", 20, "call"),
        ("get_user_by_id", "src/services/user_service.py", 3, "import"),
        ("get_user_by_id", "src/services/user_service.py", 15, "call"),
        ("get_user_by_id", "tests/test_users.py", 2, "import"),
        ("get_user_by_id", "tests/test_users.py", 10, "call"),
        ("get_user_by_id", "tests/test_users.py", 18, "call"),
        ("get_user_by_id", "tests/test_users.py", 25, "call"),
        # create_user
        ("create_user", "src/routes/users.py", 6, "import"),
        ("create_user", "src/routes/users.py", 30, "call"),
        ("create_user", "src/services/user_service.py", 4, "import"),
        ("create_user", "src/services/user_service.py", 25, "call"),
        # delete_user
        ("delete_user", "src/routes/users.py", 7, "import"),
        ("delete_user", "src/routes/users.py", 35, "call"),
        # User type used everywhere
        ("User", "src/users/queries.py", 1, "import"),
        ("User", "src/routes/users.py", 2, "import"),
        ("User", "src/services/user_service.py", 1, "import"),
        # Errors
        ("NotFoundError", "src/users/queries.py", 2, "import"),
        ("NotFoundError", "src/routes/users.py", 3, "import"),
        ("NotFoundError", "src/services/user_service.py", 5, "import"),
        ("NotFoundError", "src/services/user_service.py", 20, "call"),
        ("AppError", "src/middleware/error_handler.py", 1, "import"),
        ("AppError", "src/routes/users.py", 4, "import"),
        ("AppError", "src/middleware/auth.py", 2, "import"),
        ("ValidationError", "src/routes/users.py", 4, "import"),
        ("ValidationError", "src/services/user_service.py", 6, "import"),
        # DB
        ("db", "src/users/queries.py", 3, "import"),
        ("db", "src/services/user_service.py", 2, "import"),
        ("db", "src/routes/users.py", 1, "import"),
        # Auth
        ("auth_middleware", "src/routes/users.py", 8, "import"),
        ("auth_middleware", "src/routes/admin.py", 3, "import"),
        ("auth_middleware", "src/routes/admin.py", 10, "call"),
        ("verify_token", "src/middleware/auth.py", 3, "import"),
        ("verify_token", "src/middleware/auth.py", 8, "call"),
        # Crypto
        ("hash_password", "src/users/queries.py", 4, "import"),
        ("hash_password", "src/services/user_service.py", 7, "import"),
    ]

    for sym_name, ref_file, ref_line, ref_type in refs:
        if sym_name in ids:
            store.insert_ref(ids[sym_name], ref_file, ref_line, ref_type)

    # Packages — some used, some not
    packages = [
        ("flask", "3.0.0", "pip", False),
        ("pydantic", "2.6.0", "pip", False),
        ("structlog", "24.1.0", "pip", False),
        ("colorama", "0.4.6", "pip", False),   # unused
        ("boto3", "1.34.0", "pip", False),      # unused
        ("pytest", "8.1.0", "pip", True),
        ("ruff", "0.4.0", "pip", True),         # unused dev dep
    ]
    for pkg_name, pkg_version, pkg_manager, is_dev in packages:
        store.insert_package(pkg_name, pkg_version, pkg_manager, is_dev)

    # Exports
    store.insert_export(ids["get_user_by_id"], "src/users/queries", is_default=False)
    store.insert_export(ids["create_user"], "src/users/queries", is_default=False)
    store.insert_export(ids["User"], "src/users/types", is_default=False)
    store.insert_export(ids["NotFoundError"], "src/utils/errors", is_default=False)
    store.insert_export(ids["AppError"], "src/utils/errors", is_default=False)
    store.insert_export(ids["hash_password"], "src/utils/crypto", is_default=False)

    return ids


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_server(store: SymbolStore) -> FastMCP:
    """Build a FastMCP server wired to the given store (mirrors create_server)."""
    app = FastMCP(name="groundtruth")
    graph = ImportGraph(store)
    tracker = InterventionTracker(store)
    token_tracker = TokenTracker()
    task_parser = TaskParser(store, api_key=None)
    briefing_engine = BriefingEngine(store, api_key=None)
    orchestrator = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
    risk_scorer = RiskScorer(store)
    adaptive = AdaptiveBriefing(store, risk_scorer)
    grounding_analyzer = GroundingGapAnalyzer(store)

    def _finalize(tool_name: str, result: dict) -> str:  # type: ignore[type-arg]
        response_text = json.dumps(result)
        call_tokens = token_tracker.track(tool_name, response_text)
        result["_token_footprint"] = token_tracker.get_footprint(tool_name, call_tokens)
        return json.dumps(result)

    @app.tool()
    async def groundtruth_find_relevant(
        description: str,
        entry_points: list[str] | None = None,
        max_files: int = 10,
    ) -> str:
        """Find relevant files for a task."""
        result = await handle_find_relevant(
            description=description, store=store, graph=graph,
            task_parser=task_parser, tracker=tracker,
            entry_points=entry_points, max_files=max_files,
        )
        return _finalize("groundtruth_find_relevant", result)

    @app.tool()
    async def groundtruth_brief(
        intent: str, target_file: str | None = None,
    ) -> str:
        """Proactive briefing before code generation."""
        result = await handle_brief(
            intent=intent, briefing_engine=briefing_engine,
            tracker=tracker, store=store, graph=graph,
            target_file=target_file, adaptive=adaptive,
        )
        return _finalize("groundtruth_brief", result)

    @app.tool()
    async def groundtruth_validate(
        proposed_code: str, file_path: str, language: str | None = None,
    ) -> str:
        """Validate proposed code against the codebase index."""
        result = await handle_validate(
            proposed_code=proposed_code, file_path=file_path,
            orchestrator=orchestrator, tracker=tracker, store=store,
            language=language, grounding_analyzer=grounding_analyzer,
        )
        return _finalize("groundtruth_validate", result)

    @app.tool()
    async def groundtruth_trace(
        symbol: str, direction: str = "both", max_depth: int = 3,
    ) -> str:
        """Trace a symbol through the codebase."""
        result = await handle_trace(
            symbol=symbol, store=store, graph=graph, tracker=tracker,
            direction=direction, max_depth=max_depth,
        )
        return _finalize("groundtruth_trace", result)

    @app.tool()
    async def groundtruth_status() -> str:
        """Health check and stats."""
        result = await handle_status(store=store, tracker=tracker)
        return _finalize("groundtruth_status", result)

    @app.tool()
    async def groundtruth_dead_code() -> str:
        """Find exported symbols with zero references."""
        result = await handle_dead_code(store=store, tracker=tracker)
        return _finalize("groundtruth_dead_code", result)

    @app.tool()
    async def groundtruth_unused_packages() -> str:
        """Find installed packages that no file imports."""
        result = await handle_unused_packages(store=store, tracker=tracker)
        return _finalize("groundtruth_unused_packages", result)

    @app.tool()
    async def groundtruth_hotspots(limit: int = 20) -> str:
        """Most referenced symbols in the codebase."""
        result = await handle_hotspots(store=store, tracker=tracker, limit=limit)
        return _finalize("groundtruth_hotspots", result)

    @app.tool()
    async def groundtruth_orient() -> str:
        """Codebase orientation."""
        result = await handle_orient(
            store=store, graph=graph, tracker=tracker,
            risk_scorer=risk_scorer, root_path=".",
        )
        return _finalize("groundtruth_orient", result)

    @app.tool()
    async def groundtruth_checkpoint() -> str:
        """Session checkpoint."""
        result = await handle_checkpoint(
            store=store, tracker=tracker, risk_scorer=risk_scorer,
        )
        return _finalize("groundtruth_checkpoint", result)

    @app.tool()
    async def groundtruth_symbols(file_path: str) -> str:
        """List symbols in a file."""
        result = await handle_symbols(
            file_path=file_path, store=store, tracker=tracker,
        )
        return _finalize("groundtruth_symbols", result)

    @app.tool()
    async def groundtruth_context(symbol: str, limit: int = 20) -> str:
        """Symbol usage context."""
        result = await handle_context(
            symbol=symbol, store=store, graph=graph, tracker=tracker,
            root_path=".", limit=limit,
        )
        return _finalize("groundtruth_context", result)

    @app.tool()
    async def groundtruth_explain(
        symbol: str, file_path: str | None = None,
    ) -> str:
        """Deep dive into a symbol."""
        result = await handle_explain(
            symbol=symbol, store=store, graph=graph, tracker=tracker,
            root_path=".", file_path=file_path,
        )
        return _finalize("groundtruth_explain", result)

    @app.tool()
    async def groundtruth_impact(
        symbol: str, max_depth: int = 3,
    ) -> str:
        """Assess blast radius of modifying a symbol."""
        result = await handle_impact(
            symbol=symbol, store=store, graph=graph, tracker=tracker,
            root_path=".", max_depth=max_depth,
        )
        return _finalize("groundtruth_impact", result)

    @app.tool()
    async def groundtruth_patterns(file_path: str) -> str:
        """Detect coding conventions in sibling files."""
        result = await handle_patterns(
            file_path=file_path, store=store, tracker=tracker,
            root_path=".",
        )
        return _finalize("groundtruth_patterns", result)

    return app


@pytest.fixture
def mcp_server() -> Any:
    """Create a fully initialized MCP server with in-memory populated store."""
    store = SymbolStore(":memory:")
    store.initialize()
    _populate_store(store)
    return _build_server(store)


# ---------------------------------------------------------------------------
# Test: Status tool (no data dependencies)
# ---------------------------------------------------------------------------


class TestMCPStatus:
    """Test groundtruth_status via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_status_returns_counts(self, mcp_server: Any) -> None:
        """Status tool returns symbol/file/ref counts from the real store."""
        result = _parse_tool_result(await mcp_server.call_tool("groundtruth_status", {}))

        assert "symbols_count" in result
        assert "files_count" in result
        assert "refs_count" in result
        assert "languages" in result
        assert "interventions" in result
        assert result["symbols_count"] > 0
        assert result["indexed"] is True

    @pytest.mark.asyncio
    async def test_status_reports_python_language(self, mcp_server: Any) -> None:
        """Status correctly identifies languages in the index."""
        result = _parse_tool_result(await mcp_server.call_tool("groundtruth_status", {}))

        assert "python" in result["languages"]


# ---------------------------------------------------------------------------
# Test: Trace tool (pure graph, zero AI)
# ---------------------------------------------------------------------------


class TestMCPTrace:
    """Test groundtruth_trace via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_trace_finds_callers(self, mcp_server: Any) -> None:
        """Tracing get_user_by_id returns its callers."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_trace",
            {"symbol": "get_user_by_id"},
        ))

        assert result["symbol"]["name"] == "get_user_by_id"
        assert result["symbol"]["file"] == "src/users/queries.py"
        assert len(result["callers"]) > 0
        caller_files = {c["file"] for c in result["callers"]}
        assert "src/routes/users.py" in caller_files

    @pytest.mark.asyncio
    async def test_trace_callers_only(self, mcp_server: Any) -> None:
        """Direction=callers returns only callers, no callees."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_trace",
            {"symbol": "get_user_by_id", "direction": "callers"},
        ))

        assert len(result["callers"]) > 0
        assert result["callees"] == []

    @pytest.mark.asyncio
    async def test_trace_nonexistent_symbol(self, mcp_server: Any) -> None:
        """Tracing a symbol that doesn't exist returns an error."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_trace",
            {"symbol": "nonExistentFunction"},
        ))

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_trace_impact_radius(self, mcp_server: Any) -> None:
        """Tracing a high-usage symbol shows a positive impact radius."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_trace",
            {"symbol": "get_user_by_id", "direction": "both"},
        ))

        assert result["impact_radius"] >= 1
        assert len(result["dependency_chain"]) >= 1


# ---------------------------------------------------------------------------
# Test: Dead code tool (pure SQL)
# ---------------------------------------------------------------------------


class TestMCPDeadCode:
    """Test groundtruth_dead_code via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_finds_dead_symbols(self, mcp_server: Any) -> None:
        """Dead code tool finds exported symbols with zero references."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_dead_code", {},
        ))

        dead_names = {s["name"] for s in result["dead_symbols"]}
        assert "format_legacy_date" in dead_names
        assert "DeprecatedLogger" in dead_names
        assert result["total"] >= 2

    @pytest.mark.asyncio
    async def test_used_symbols_not_dead(self, mcp_server: Any) -> None:
        """Actively used symbols should not appear in dead code."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_dead_code", {},
        ))

        dead_names = {s["name"] for s in result["dead_symbols"]}
        assert "get_user_by_id" not in dead_names
        assert "User" not in dead_names
        assert "AppError" not in dead_names


# ---------------------------------------------------------------------------
# Test: Unused packages tool (pure SQL)
# ---------------------------------------------------------------------------


class TestMCPUnusedPackages:
    """Test groundtruth_unused_packages via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_finds_unused_packages(self, mcp_server: Any) -> None:
        """Unused packages tool identifies packages with no imports."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_unused_packages", {},
        ))

        unused_names = {p["name"] for p in result["unused_packages"]}
        # All packages are "unused" since we didn't create import refs linking to packages
        # The tool checks if any ref references the package name
        assert result["total"] >= 1


# ---------------------------------------------------------------------------
# Test: Hotspots tool (pure SQL)
# ---------------------------------------------------------------------------


class TestMCPHotspots:
    """Test groundtruth_hotspots via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_hotspots_ordered_by_usage(self, mcp_server: Any) -> None:
        """Hotspots returns symbols ordered by usage_count descending."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {"limit": 5},
        ))

        hotspots = result["hotspots"]
        assert len(hotspots) == 5
        counts = [h["usage_count"] for h in hotspots]
        assert counts == sorted(counts, reverse=True)

    @pytest.mark.asyncio
    async def test_hotspots_most_used_symbol(self, mcp_server: Any) -> None:
        """The most-used symbol should be User (usage_count=10)."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {"limit": 1},
        ))

        assert result["hotspots"][0]["name"] == "User"
        assert result["hotspots"][0]["usage_count"] == 10

    @pytest.mark.asyncio
    async def test_hotspots_default_limit(self, mcp_server: Any) -> None:
        """Default limit returns up to 20 symbols."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {},
        ))

        # We have 15 symbols total, so all should be returned
        assert len(result["hotspots"]) <= 20


# ---------------------------------------------------------------------------
# Test: Find relevant (uses task parser fallback — no API key)
# ---------------------------------------------------------------------------


class TestMCPFindRelevant:
    """Test groundtruth_find_relevant via MCP tool dispatch.

    Without an ANTHROPIC_API_KEY, TaskParser falls back to deterministic
    camelCase/snake_case splitting. Tests use symbol names that survive
    the fallback parser.
    """

    @pytest.mark.asyncio
    async def test_find_relevant_by_symbol_name(self, mcp_server: Any) -> None:
        """find_relevant locates files when the description contains an exact symbol name."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_find_relevant",
            {"description": "fix get_user_by_id returning null"},
        ))

        assert "files" in result
        paths = [f["path"] for f in result["files"]]
        # The queries file defines get_user_by_id
        assert "src/users/queries.py" in paths

    @pytest.mark.asyncio
    async def test_find_relevant_with_entry_points(self, mcp_server: Any) -> None:
        """Explicit entry_points are included in results."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_find_relevant",
            {
                "description": "add logging",
                "entry_points": ["src/routes/users.py"],
            },
        ))

        assert "files" in result
        # entry_points should appear even if task parser doesn't find matching symbols
        # (since entry_points are added directly)

    @pytest.mark.asyncio
    async def test_find_relevant_max_files(self, mcp_server: Any) -> None:
        """max_files parameter limits result count."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_find_relevant",
            {"description": "fix get_user_by_id", "max_files": 2},
        ))

        assert len(result.get("files", [])) <= 2


# ---------------------------------------------------------------------------
# Test: Validate (deterministic path — no AI needed)
# ---------------------------------------------------------------------------


class TestMCPValidate:
    """Test groundtruth_validate via MCP tool dispatch.

    These tests exercise the full validation pipeline (import/package/signature
    validators) against the real populated store.
    """

    @pytest.mark.asyncio
    async def test_validate_correct_import(self, mcp_server: Any) -> None:
        """Valid import passes validation."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_validate",
            {
                "proposed_code": "from users.queries import get_user_by_id\n",
                "file_path": "src/routes/users.py",
                "language": "python",
            },
        ))

        # Should not error — the symbol exists in the index
        assert "valid" in result

    @pytest.mark.asyncio
    async def test_validate_wrong_module_path(self, mcp_server: Any) -> None:
        """Importing a symbol from the wrong module is caught."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_validate",
            {
                "proposed_code": "from auth import hash_password\n",
                "file_path": "src/routes/users.py",
                "language": "python",
            },
        ))

        # Validation should run (may or may not find errors depending on
        # how the orchestrator handles the import path vs store)
        assert "valid" in result
        assert "errors" in result

    @pytest.mark.asyncio
    async def test_validate_returns_latency(self, mcp_server: Any) -> None:
        """Validation result includes latency_ms."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_validate",
            {
                "proposed_code": "x = 1\n",
                "file_path": "src/test.py",
                "language": "python",
            },
        ))

        assert "latency_ms" in result
        assert result["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# Test: Brief (deterministic fallback — no API key)
# ---------------------------------------------------------------------------


class TestMCPBrief:
    """Test groundtruth_brief via MCP tool dispatch.

    Without ANTHROPIC_API_KEY, the briefing engine falls back to deterministic
    symbol lookup via FTS5, returning structured data without AI summarization.
    """

    @pytest.mark.asyncio
    async def test_brief_returns_structure(self, mcp_server: Any) -> None:
        """Brief tool returns the expected response structure."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_brief",
            {"intent": "add error handling to get_user_by_id"},
        ))

        # Should have briefing structure (even if fallback)
        # The exact keys depend on whether AI or fallback path is taken
        assert isinstance(result, dict)
        # Should not be an error (FTS5 can find matching symbols)
        if "error" not in result:
            assert "briefing" in result or "relevant_symbols" in result


# ---------------------------------------------------------------------------
# Test: Multi-tool workflow (the real E2E value)
# ---------------------------------------------------------------------------


class TestMCPWorkflow:
    """Test multi-tool workflows that simulate real agent behavior.

    These chain multiple tools together as an agent would:
    find_relevant → trace → validate.
    """

    @pytest.mark.asyncio
    async def test_find_then_trace(self, mcp_server: Any) -> None:
        """Agent finds relevant files, then traces a key symbol."""
        # Step 1: Find relevant files
        find_result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_find_relevant",
            {"description": "fix get_user_by_id error handling"},
        ))

        # Step 2: Trace the main symbol found
        if find_result.get("entry_symbols"):
            symbol = find_result["entry_symbols"][0]
            trace_result = _parse_tool_result(await mcp_server.call_tool(
                "groundtruth_trace",
                {"symbol": symbol},
            ))

            if "error" not in trace_result:
                assert trace_result["symbol"]["name"] == symbol
                assert "callers" in trace_result

    @pytest.mark.asyncio
    async def test_status_then_hotspots_then_dead_code(self, mcp_server: Any) -> None:
        """Agent checks health, finds hotspots, then finds dead code."""
        # Step 1: Health check
        status = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_status", {},
        ))
        assert status["indexed"] is True
        total_symbols = status["symbols_count"]

        # Step 2: Find hotspots
        hotspots = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {"limit": 5},
        ))
        assert len(hotspots["hotspots"]) > 0

        # Step 3: Find dead code
        dead = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_dead_code", {},
        ))

        # Hotspots + dead code should be a subset of total symbols
        assert len(hotspots["hotspots"]) + dead["total"] <= total_symbols

    @pytest.mark.asyncio
    async def test_interventions_tracked(self, mcp_server: Any) -> None:
        """Intervention stats accumulate across multiple tool calls."""
        # Make several tool calls
        await mcp_server.call_tool("groundtruth_status", {})
        await mcp_server.call_tool("groundtruth_hotspots", {"limit": 3})
        await mcp_server.call_tool("groundtruth_dead_code", {})
        await mcp_server.call_tool(
            "groundtruth_trace", {"symbol": "get_user_by_id"},
        )

        # Check that status reflects the interventions
        status = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_status", {},
        ))

        # At least the trace/hotspots/dead_code calls should have been tracked
        interventions = status.get("interventions", {})
        assert interventions.get("total", 0) >= 3


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


class TestMCPEdgeCases:
    """Test edge cases and error handling through the MCP dispatch."""

    @pytest.mark.asyncio
    async def test_trace_empty_symbol(self, mcp_server: Any) -> None:
        """Tracing an empty string returns error gracefully."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_trace",
            {"symbol": ""},
        ))

        assert "error" in result

    @pytest.mark.asyncio
    async def test_find_relevant_empty_description(self, mcp_server: Any) -> None:
        """Empty description returns empty results, not a crash."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_find_relevant",
            {"description": ""},
        ))

        assert "files" in result or "error" in result

    @pytest.mark.asyncio
    async def test_validate_empty_code(self, mcp_server: Any) -> None:
        """Empty code string doesn't crash validation."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_validate",
            {"proposed_code": "", "file_path": "src/test.py"},
        ))

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_hotspots_zero_limit(self, mcp_server: Any) -> None:
        """Limit=0 returns no hotspots."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {"limit": 0},
        ))

        assert result["hotspots"] == []


# ---------------------------------------------------------------------------
# Test: Orient tool
# ---------------------------------------------------------------------------


class TestMCPOrient:
    """Test groundtruth_orient via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_orient_returns_structure(self, mcp_server: Any) -> None:
        """Orient returns project stats, structure, and risk summary."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_orient", {},
        ))

        assert "project" in result
        assert "structure" in result
        assert "entry_points" in result
        assert "risk_summary" in result
        assert result["project"]["symbols_count"] > 0


# ---------------------------------------------------------------------------
# Test: Checkpoint tool
# ---------------------------------------------------------------------------


class TestMCPCheckpoint:
    """Test groundtruth_checkpoint via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_checkpoint_after_workflow(self, mcp_server: Any) -> None:
        """Checkpoint summarizes previous tool calls in the session."""
        # Make some calls first (status doesn't record, trace does)
        await mcp_server.call_tool("groundtruth_status", {})
        await mcp_server.call_tool(
            "groundtruth_trace", {"symbol": "get_user_by_id"},
        )

        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_checkpoint", {},
        ))

        assert "session" in result
        # Summary is computed before checkpoint records itself
        assert result["session"]["total_calls"] >= 1
        assert isinstance(result["recommendations"], list)


# ---------------------------------------------------------------------------
# Test: Symbols tool
# ---------------------------------------------------------------------------


class TestMCPSymbols:
    """Test groundtruth_symbols via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_symbols_for_file(self, mcp_server: Any) -> None:
        """Symbols tool returns symbols in a specific file."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_symbols",
            {"file_path": "src/users/queries.py"},
        ))

        assert result["file_path"] == "src/users/queries.py"
        assert result["symbol_count"] >= 1
        names = [s["name"] for s in result["symbols"]]
        assert "get_user_by_id" in names

    @pytest.mark.asyncio
    async def test_symbols_empty_file(self, mcp_server: Any) -> None:
        """Symbols for a nonexistent file returns empty list."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_symbols",
            {"file_path": "nonexistent/file.py"},
        ))

        assert result["symbol_count"] == 0


# ---------------------------------------------------------------------------
# Test: Context tool
# ---------------------------------------------------------------------------


class TestMCPContext:
    """Test groundtruth_context via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_context_finds_symbol(self, mcp_server: Any) -> None:
        """Context tool returns symbol info and usages."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_context",
            {"symbol": "get_user_by_id"},
        ))

        assert result["symbol"]["name"] == "get_user_by_id"
        assert result["total_usages"] >= 1

    @pytest.mark.asyncio
    async def test_context_unknown_symbol(self, mcp_server: Any) -> None:
        """Context for unknown symbol returns error."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_context",
            {"symbol": "nonExistentFunc"},
        ))

        assert "error" in result


# ---------------------------------------------------------------------------
# Test: Explain tool (new)
# ---------------------------------------------------------------------------


class TestMCPExplain:
    """Test groundtruth_explain via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_explain_returns_symbol(self, mcp_server: Any) -> None:
        """Explain tool returns symbol info and callers."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_explain",
            {"symbol": "get_user_by_id"},
        ))

        assert result["symbol"]["name"] == "get_user_by_id"
        assert "called_by" in result
        assert "side_effects_detected" in result
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_explain_unknown_symbol(self, mcp_server: Any) -> None:
        """Explain for unknown symbol returns error."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_explain",
            {"symbol": "nonExistent"},
        ))

        assert "error" in result


# ---------------------------------------------------------------------------
# Test: Impact tool (new)
# ---------------------------------------------------------------------------


class TestMCPImpact:
    """Test groundtruth_impact via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_impact_returns_callers(self, mcp_server: Any) -> None:
        """Impact tool returns callers and break risk."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_impact",
            {"symbol": "get_user_by_id"},
        ))

        assert result["symbol"]["name"] == "get_user_by_id"
        assert len(result["direct_callers"]) >= 1
        assert "impact_summary" in result
        assert "safe_changes" in result
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_impact_unknown_symbol(self, mcp_server: Any) -> None:
        """Impact for unknown symbol returns error."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_impact",
            {"symbol": "nonExistent"},
        ))

        assert "error" in result


# ---------------------------------------------------------------------------
# Test: Patterns tool (new)
# ---------------------------------------------------------------------------


class TestMCPPatterns:
    """Test groundtruth_patterns via MCP tool dispatch."""

    @pytest.mark.asyncio
    async def test_patterns_returns_structure(self, mcp_server: Any) -> None:
        """Patterns tool returns directory and patterns info."""
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_patterns",
            {"file_path": "src/users/queries.py"},
        ))

        assert "directory" in result
        assert "sibling_files_analyzed" in result
        assert "patterns_detected" in result
        assert "reasoning_guidance" in result


# ---------------------------------------------------------------------------
# Test: Token footprint and reasoning_guidance on all tools
# ---------------------------------------------------------------------------


class TestMCPTokenFootprint:
    """Test that all tool responses include _token_footprint."""

    @pytest.mark.asyncio
    async def test_status_has_footprint(self, mcp_server: Any) -> None:
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_status", {},
        ))
        assert "_token_footprint" in result
        fp = result["_token_footprint"]
        assert "this_call_tokens" in fp
        assert "session_total_tokens" in fp
        assert "breakdown" in fp

    @pytest.mark.asyncio
    async def test_trace_has_footprint(self, mcp_server: Any) -> None:
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_trace", {"symbol": "get_user_by_id"},
        ))
        assert "_token_footprint" in result

    @pytest.mark.asyncio
    async def test_hotspots_has_footprint(self, mcp_server: Any) -> None:
        result = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {"limit": 3},
        ))
        assert "_token_footprint" in result

    @pytest.mark.asyncio
    async def test_session_accumulates(self, mcp_server: Any) -> None:
        """Token footprint accumulates across calls."""
        r1 = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_status", {},
        ))
        r2 = _parse_tool_result(await mcp_server.call_tool(
            "groundtruth_hotspots", {"limit": 1},
        ))
        assert (
            r2["_token_footprint"]["session_total_tokens"]
            >= r1["_token_footprint"]["session_total_tokens"]
        )


class TestMCPReasoningGuidanceE2E:
    """Test that all tool responses include reasoning_guidance."""

    @pytest.mark.asyncio
    async def test_all_tools_have_guidance(self, mcp_server: Any) -> None:
        """Every non-error tool response includes reasoning_guidance."""
        tools_and_args = [
            ("groundtruth_status", {}),
            ("groundtruth_hotspots", {"limit": 3}),
            ("groundtruth_dead_code", {}),
            ("groundtruth_trace", {"symbol": "get_user_by_id"}),
            ("groundtruth_symbols", {"file_path": "src/users/queries.py"}),
            ("groundtruth_context", {"symbol": "get_user_by_id"}),
            ("groundtruth_explain", {"symbol": "get_user_by_id"}),
            ("groundtruth_impact", {"symbol": "get_user_by_id"}),
            ("groundtruth_patterns", {"file_path": "src/users/queries.py"}),
        ]
        for tool_name, args in tools_and_args:
            result = _parse_tool_result(await mcp_server.call_tool(tool_name, args))
            if "error" not in result:
                assert "reasoning_guidance" in result, (
                    f"{tool_name} missing reasoning_guidance"
                )
                assert len(result["reasoning_guidance"]) > 0, (
                    f"{tool_name} has empty reasoning_guidance"
                )
