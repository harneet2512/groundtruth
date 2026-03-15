"""Cross-language integration tests.

Tests the store/graph/validator/tool pipeline with pre-populated SQLite data
that simulates what indexing would produce for each fixture project (TS, Py, Go).
No real LSP servers needed.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from groundtruth.ai.task_parser import TaskParser
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools import (
    handle_dead_code,
    handle_find_relevant,
    handle_hotspots,
    handle_trace,
    handle_unused_packages,
    handle_validate,
)
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok
from groundtruth.validators.orchestrator import ValidationOrchestrator, ValidationResult


# ---------------------------------------------------------------------------
# Fixture data for each language
# ---------------------------------------------------------------------------

_TS_SYMBOLS: list[dict[str, Any]] = [
    {"name": "getUserById", "kind": "function", "file_path": "src/users/queries.ts",
     "line_number": 5, "end_line": 15, "is_exported": True,
     "signature": "(userId: number) => Promise<User>", "return_type": "Promise<User>",
     "usage_count": 3},
    {"name": "createUser", "kind": "function", "file_path": "src/users/queries.ts",
     "line_number": 17, "end_line": 25, "is_exported": True,
     "signature": "(input: CreateUserInput) => Promise<User>", "return_type": "Promise<User>",
     "usage_count": 2},
    {"name": "User", "kind": "interface", "file_path": "src/users/types.ts",
     "line_number": 1, "end_line": 8, "is_exported": True, "usage_count": 5},
    {"name": "NotFoundError", "kind": "class", "file_path": "src/utils/errors.ts",
     "line_number": 10, "end_line": 18, "is_exported": True, "usage_count": 2},
    {"name": "AppError", "kind": "class", "file_path": "src/utils/errors.ts",
     "line_number": 1, "end_line": 9, "is_exported": True, "usage_count": 3},
    {"name": "hashPassword", "kind": "function", "file_path": "src/utils/crypto.ts",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(password: string) => Promise<string>", "usage_count": 1},
    {"name": "authMiddleware", "kind": "function", "file_path": "src/middleware/auth.ts",
     "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 2},
    {"name": "db", "kind": "variable", "file_path": "src/db/client.ts",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 4},
    {"name": "formatLegacyDate", "kind": "function", "file_path": "src/utils/dates.ts",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 0},
]

_PY_SYMBOLS: list[dict[str, Any]] = [
    {"name": "get_user_by_id", "kind": "function", "file_path": "src/users/queries.py",
     "line_number": 5, "end_line": 15, "is_exported": True,
     "signature": "(user_id: int) -> User", "return_type": "User",
     "usage_count": 3},
    {"name": "create_user", "kind": "function", "file_path": "src/users/queries.py",
     "line_number": 17, "end_line": 25, "is_exported": True,
     "signature": "(input: CreateUserInput) -> User", "return_type": "User",
     "usage_count": 2},
    {"name": "User", "kind": "class", "file_path": "src/users/types.py",
     "line_number": 1, "end_line": 8, "is_exported": True, "usage_count": 5},
    {"name": "NotFoundError", "kind": "class", "file_path": "src/utils/errors.py",
     "line_number": 10, "end_line": 18, "is_exported": True, "usage_count": 2},
    {"name": "AppError", "kind": "class", "file_path": "src/utils/errors.py",
     "line_number": 1, "end_line": 9, "is_exported": True, "usage_count": 3},
    {"name": "hash_password", "kind": "function", "file_path": "src/utils/crypto.py",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(password: str) -> str", "usage_count": 1},
    {"name": "auth_middleware", "kind": "function", "file_path": "src/middleware/auth.py",
     "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 2},
    {"name": "db", "kind": "variable", "file_path": "src/db/client.py",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 4},
    {"name": "format_legacy_date", "kind": "function", "file_path": "src/utils/dates.py",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 0},
]

_GO_SYMBOLS: list[dict[str, Any]] = [
    {"name": "GetUserByID", "kind": "function", "file_path": "users/queries.go",
     "line_number": 5, "end_line": 15, "is_exported": True,
     "signature": "(userID int) (*User, error)", "return_type": "*User",
     "usage_count": 3},
    {"name": "CreateUser", "kind": "function", "file_path": "users/queries.go",
     "line_number": 17, "end_line": 25, "is_exported": True,
     "signature": "(input CreateUserInput) (*User, error)", "return_type": "*User",
     "usage_count": 2},
    {"name": "User", "kind": "type", "file_path": "users/types.go",
     "line_number": 1, "end_line": 8, "is_exported": True, "usage_count": 5},
    {"name": "NotFoundError", "kind": "type", "file_path": "utils/errors.go",
     "line_number": 10, "end_line": 18, "is_exported": True, "usage_count": 2},
    {"name": "AppError", "kind": "type", "file_path": "utils/errors.go",
     "line_number": 1, "end_line": 9, "is_exported": True, "usage_count": 3},
    {"name": "HashPassword", "kind": "function", "file_path": "utils/crypto.go",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(password string) (string, error)", "usage_count": 1},
    {"name": "AuthMiddleware", "kind": "function", "file_path": "middleware/auth.go",
     "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 2},
    {"name": "DB", "kind": "variable", "file_path": "db/client.go",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 4},
    {"name": "FormatLegacyDate", "kind": "function", "file_path": "utils/dates.go",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 0},
]

_TS_REFS: list[dict[str, Any]] = [
    {"symbol_name": "getUserById", "referenced_in_file": "src/routes/users.ts",
     "referenced_at_line": 3, "reference_type": "import"},
    {"symbol_name": "getUserById", "referenced_in_file": "src/routes/users.ts",
     "referenced_at_line": 15, "reference_type": "call"},
    {"symbol_name": "getUserById", "referenced_in_file": "src/index.ts",
     "referenced_at_line": 10, "reference_type": "call"},
    {"symbol_name": "NotFoundError", "referenced_in_file": "src/users/queries.ts",
     "referenced_at_line": 2, "reference_type": "import"},
    {"symbol_name": "db", "referenced_in_file": "src/users/queries.ts",
     "referenced_at_line": 1, "reference_type": "import"},
    {"symbol_name": "AppError", "referenced_in_file": "src/middleware/errorHandler.ts",
     "referenced_at_line": 1, "reference_type": "import"},
]

_PY_REFS: list[dict[str, Any]] = [
    {"symbol_name": "get_user_by_id", "referenced_in_file": "src/routes/users.py",
     "referenced_at_line": 3, "reference_type": "import"},
    {"symbol_name": "get_user_by_id", "referenced_in_file": "src/routes/users.py",
     "referenced_at_line": 15, "reference_type": "call"},
    {"symbol_name": "get_user_by_id", "referenced_in_file": "src/app.py",
     "referenced_at_line": 10, "reference_type": "call"},
    {"symbol_name": "NotFoundError", "referenced_in_file": "src/users/queries.py",
     "referenced_at_line": 2, "reference_type": "import"},
    {"symbol_name": "db", "referenced_in_file": "src/users/queries.py",
     "referenced_at_line": 1, "reference_type": "import"},
    {"symbol_name": "AppError", "referenced_in_file": "src/middleware/error_handler.py",
     "referenced_at_line": 1, "reference_type": "import"},
]

_GO_REFS: list[dict[str, Any]] = [
    {"symbol_name": "GetUserByID", "referenced_in_file": "handlers/users.go",
     "referenced_at_line": 3, "reference_type": "import"},
    {"symbol_name": "GetUserByID", "referenced_in_file": "handlers/users.go",
     "referenced_at_line": 15, "reference_type": "call"},
    {"symbol_name": "GetUserByID", "referenced_in_file": "main.go",
     "referenced_at_line": 10, "reference_type": "call"},
    {"symbol_name": "NotFoundError", "referenced_in_file": "users/queries.go",
     "referenced_at_line": 2, "reference_type": "import"},
    {"symbol_name": "DB", "referenced_in_file": "users/queries.go",
     "referenced_at_line": 1, "reference_type": "import"},
    {"symbol_name": "AppError", "referenced_in_file": "middleware/error_handler.go",
     "referenced_at_line": 1, "reference_type": "import"},
]

_LANG_CONFIG: dict[str, dict[str, Any]] = {
    "typescript": {
        "language": "typescript",
        "symbols": _TS_SYMBOLS,
        "refs": _TS_REFS,
        "packages": [
            ("express", "4.18.0", "npm"),
            ("zod", "3.22.0", "npm"),
            ("axios", "1.6.0", "npm"),  # unused
        ],
        "query_func": "getUserById",
        "error_class": "NotFoundError",
        "dead_symbol": "formatLegacyDate",
        "unused_pkg": "axios",
        "queries_file": "src/users/queries.ts",
        "errors_file": "src/utils/errors.ts",
        "db_file": "src/db/client.ts",
    },
    "python": {
        "language": "python",
        "symbols": _PY_SYMBOLS,
        "refs": _PY_REFS,
        "packages": [
            ("flask", "3.0.0", "pip"),
            ("pydantic", "2.0.0", "pip"),
            ("requests", "2.31.0", "pip"),  # unused
        ],
        "query_func": "get_user_by_id",
        "error_class": "NotFoundError",
        "dead_symbol": "format_legacy_date",
        "unused_pkg": "requests",
        "queries_file": "src/users/queries.py",
        "errors_file": "src/utils/errors.py",
        "db_file": "src/db/client.py",
    },
    "go": {
        "language": "go",
        "symbols": _GO_SYMBOLS,
        "refs": _GO_REFS,
        "packages": [
            ("gin", "1.9.0", "go"),
            ("gorm", "1.25.0", "go"),
            ("fiber", "2.0.0", "go"),  # unused
        ],
        "query_func": "GetUserByID",
        "error_class": "NotFoundError",
        "dead_symbol": "FormatLegacyDate",
        "unused_pkg": "fiber",
        "queries_file": "users/queries.go",
        "errors_file": "utils/errors.go",
        "db_file": "db/client.go",
    },
}


def _populate_store(store: SymbolStore, config: dict[str, Any]) -> dict[str, int]:
    """Populate a store with symbols, refs, and packages. Returns name→id map."""
    now = int(time.time())
    name_to_id: dict[str, int] = {}
    lang = config["language"]

    for sym in config["symbols"]:
        result = store.insert_symbol(
            name=sym["name"],
            kind=sym["kind"],
            language=lang,
            file_path=sym["file_path"],
            line_number=sym["line_number"],
            end_line=sym["end_line"],
            is_exported=sym["is_exported"],
            signature=sym.get("signature"),
            params=None,
            return_type=sym.get("return_type"),
            documentation=None,
            last_indexed_at=now,
        )
        assert isinstance(result, Ok)
        sid = result.value
        name_to_id[sym["name"]] = sid
        if sym.get("usage_count", 0) > 0:
            store.update_usage_count(sid, sym["usage_count"])

    for ref in config["refs"]:
        sym_id = name_to_id[ref["symbol_name"]]
        store.insert_ref(
            sym_id, ref["referenced_in_file"],
            ref["referenced_at_line"], ref["reference_type"],
        )

    for pkg_name, pkg_version, pkg_manager in config["packages"]:
        store.insert_package(pkg_name, pkg_version, pkg_manager)

    return name_to_id


# ---------------------------------------------------------------------------
# Parameterized tests
# ---------------------------------------------------------------------------


@pytest.fixture(params=["typescript", "python", "go"])
def lang_ctx(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Create a populated store for each language."""
    lang = request.param
    config = _LANG_CONFIG[lang]
    store = SymbolStore(":memory:")
    store.initialize()
    name_to_id = _populate_store(store, config)
    graph = ImportGraph(store)
    tracker = InterventionTracker(store)
    return {
        "store": store,
        "graph": graph,
        "tracker": tracker,
        "config": config,
        "name_to_id": name_to_id,
        "language": lang,
    }


class TestFindRelevant:
    @pytest.mark.asyncio
    async def test_finds_query_and_error_files(self, lang_ctx: dict[str, Any]) -> None:
        """find_relevant returns the queries file and errors file for the query function."""
        config = lang_ctx["config"]
        task_parser = MagicMock(spec=TaskParser)
        task_parser.parse = AsyncMock(
            return_value=Ok([config["query_func"], config["error_class"]])
        )

        result = await handle_find_relevant(
            description=f"fix {config['query_func']} returning null",
            store=lang_ctx["store"],
            graph=lang_ctx["graph"],
            task_parser=task_parser,
            tracker=lang_ctx["tracker"],
        )

        assert "files" in result
        paths = [f["path"] for f in result["files"]]
        assert config["queries_file"] in paths
        assert config["errors_file"] in paths
        assert config["query_func"] in result["entry_symbols"]


class TestTrace:
    @pytest.mark.asyncio
    async def test_trace_query_function(self, lang_ctx: dict[str, Any]) -> None:
        """Tracing the query function returns callers."""
        config = lang_ctx["config"]

        result = await handle_trace(
            symbol=config["query_func"],
            store=lang_ctx["store"],
            graph=lang_ctx["graph"],
            tracker=lang_ctx["tracker"],
        )

        assert result["symbol"]["name"] == config["query_func"]
        assert result["symbol"]["file"] == config["queries_file"]
        assert len(result["callers"]) > 0
        assert isinstance(result["impact_radius"], int)
        assert result["impact_radius"] >= 1


class TestDeadCode:
    @pytest.mark.asyncio
    async def test_finds_dead_symbol(self, lang_ctx: dict[str, Any]) -> None:
        """Dead code detection finds the unused exported symbol."""
        config = lang_ctx["config"]

        result = await handle_dead_code(
            store=lang_ctx["store"],
            tracker=lang_ctx["tracker"],
        )

        dead_names = {s["name"] for s in result["dead_symbols"]}
        assert config["dead_symbol"] in dead_names
        # Used symbols should NOT be in dead code
        assert config["query_func"] not in dead_names
        assert result["total"] >= 1


class TestUnusedPackages:
    @pytest.mark.asyncio
    async def test_finds_unused_package(self, lang_ctx: dict[str, Any]) -> None:
        """Unused package detection finds the intentionally unused package."""
        config = lang_ctx["config"]

        result = await handle_unused_packages(
            store=lang_ctx["store"],
            tracker=lang_ctx["tracker"],
        )

        unused_names = {p["name"] for p in result["unused_packages"]}
        assert config["unused_pkg"] in unused_names
        assert result["total"] >= 1


class TestHotspots:
    @pytest.mark.asyncio
    async def test_hotspots_ordered(self, lang_ctx: dict[str, Any]) -> None:
        """Hotspots returns symbols ordered by usage count."""
        result = await handle_hotspots(
            store=lang_ctx["store"],
            tracker=lang_ctx["tracker"],
        )

        hotspots = result["hotspots"]
        assert len(hotspots) > 0
        # Verify ordering: descending by usage_count
        counts = [h["usage_count"] for h in hotspots]
        assert counts == sorted(counts, reverse=True)
        # The most-used symbol should be User (usage_count=5) or db (4)
        assert hotspots[0]["usage_count"] >= 4

    @pytest.mark.asyncio
    async def test_hotspots_limit(self, lang_ctx: dict[str, Any]) -> None:
        """Limit parameter works across languages."""
        result = await handle_hotspots(
            store=lang_ctx["store"],
            tracker=lang_ctx["tracker"],
            limit=2,
        )
        assert len(result["hotspots"]) == 2


class TestValidateWrongImport:
    @pytest.mark.asyncio
    async def test_wrong_import_detected(self, lang_ctx: dict[str, Any]) -> None:
        """Validation catches wrong module path imports."""
        config = lang_ctx["config"]
        orchestrator = MagicMock(spec=ValidationOrchestrator)
        orchestrator.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=False,
            errors=[{
                "type": "wrong_module_path",
                "message": f"{config['query_func']} not found in auth/",
                "suggestion": {
                    "source": "deterministic",
                    "fix": f"import from {config['queries_file']}",
                    "confidence": 0.95,
                },
            }],
            ai_used=False,
            latency_ms=5,
        )))

        result = await handle_validate(
            proposed_code=f"import {config['query_func']} from auth",
            file_path="src/routes/users.ts",
            orchestrator=orchestrator,
            tracker=lang_ctx["tracker"],
            store=lang_ctx["store"],
        )

        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["type"] == "wrong_module_path"

    @pytest.mark.asyncio
    async def test_valid_code_passes(self, lang_ctx: dict[str, Any]) -> None:
        """Valid code passes validation."""
        orchestrator = MagicMock(spec=ValidationOrchestrator)
        orchestrator.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=True, errors=[], ai_used=False, latency_ms=3,
        )))

        result = await handle_validate(
            proposed_code="import { getUserById } from './users/queries'",
            file_path="src/routes/users.ts",
            orchestrator=orchestrator,
            tracker=lang_ctx["tracker"],
            store=lang_ctx["store"],
        )

        assert result["valid"] is True
        assert result["errors"] == []
