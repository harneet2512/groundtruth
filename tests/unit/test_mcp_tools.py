"""Tests for MCP tool handlers."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from groundtruth.ai.briefing import BriefingEngine, BriefingResult
from groundtruth.ai.task_parser import TaskParser
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.mcp.tools import (
    handle_brief,
    handle_checkpoint,
    handle_context,
    handle_dead_code,
    handle_find_relevant,
    handle_hotspots,
    handle_orient,
    handle_status,
    handle_symbols,
    handle_trace,
    handle_unused_packages,
    handle_validate,
)
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok
from groundtruth.validators.orchestrator import ValidationOrchestrator, ValidationResult


def _setup() -> dict[str, Any]:
    """Create a populated store and real graph/tracker."""
    store = SymbolStore(":memory:")
    store.initialize()

    now = int(time.time())

    # Insert symbols
    r1 = store.insert_symbol(
        name="getUserById", kind="function", language="python",
        file_path="src/users/queries.py", line_number=10, end_line=20,
        is_exported=True, signature="(user_id: int) -> User",
        params=None, return_type="User", documentation="Get a user by ID.",
        last_indexed_at=now,
    )
    assert isinstance(r1, Ok)
    sym1_id = r1.value

    r2 = store.insert_symbol(
        name="NotFoundError", kind="class", language="python",
        file_path="src/utils/errors.py", line_number=5, end_line=10,
        is_exported=True, signature=None,
        params=None, return_type=None, documentation="Not found error.",
        last_indexed_at=now,
    )
    assert isinstance(r2, Ok)
    sym2_id = r2.value

    r3 = store.insert_symbol(
        name="handle_users", kind="function", language="python",
        file_path="src/routes/users.py", line_number=1, end_line=30,
        is_exported=True, signature="(request) -> Response",
        params=None, return_type="Response", documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r3, Ok)

    # Insert refs: routes/users.py imports getUserById
    store.insert_ref(sym1_id, "src/routes/users.py", 3, "import")
    # Insert refs: routes/users.py calls getUserById
    store.insert_ref(sym1_id, "src/routes/users.py", 15, "call")

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    return {
        "store": store,
        "graph": graph,
        "tracker": tracker,
        "sym1_id": sym1_id,
        "sym2_id": sym2_id,
    }


class TestHandleFindRelevant:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        ctx = _setup()
        task_parser = MagicMock(spec=TaskParser)
        task_parser.parse = AsyncMock(return_value=Ok(["getUserById"]))

        result = await handle_find_relevant(
            description="fix getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            task_parser=task_parser,
            tracker=ctx["tracker"],
        )

        assert "files" in result
        assert len(result["files"]) > 0
        paths = [f["path"] for f in result["files"]]
        assert "src/users/queries.py" in paths
        assert "getUserById" in result["entry_symbols"]

    @pytest.mark.asyncio
    async def test_unknown_symbols(self) -> None:
        ctx = _setup()
        task_parser = MagicMock(spec=TaskParser)
        task_parser.parse = AsyncMock(return_value=Ok(["nonExistentSymbol"]))

        result = await handle_find_relevant(
            description="fix nonExistentSymbol",
            store=ctx["store"],
            graph=ctx["graph"],
            task_parser=task_parser,
            tracker=ctx["tracker"],
        )

        assert result["files"] == []

    @pytest.mark.asyncio
    async def test_entry_points_override(self) -> None:
        ctx = _setup()
        task_parser = MagicMock(spec=TaskParser)
        task_parser.parse = AsyncMock(return_value=Ok([]))

        result = await handle_find_relevant(
            description="something",
            store=ctx["store"],
            graph=ctx["graph"],
            task_parser=task_parser,
            tracker=ctx["tracker"],
            entry_points=["src/users/queries.py"],
        )

        assert len(result["files"]) > 0
        paths = [f["path"] for f in result["files"]]
        assert "src/users/queries.py" in paths

    @pytest.mark.asyncio
    async def test_max_files_truncation(self) -> None:
        ctx = _setup()
        task_parser = MagicMock(spec=TaskParser)
        task_parser.parse = AsyncMock(return_value=Ok(["getUserById"]))

        result = await handle_find_relevant(
            description="fix getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            task_parser=task_parser,
            tracker=ctx["tracker"],
            max_files=1,
        )

        assert len(result["files"]) == 1


class TestHandleBrief:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        ctx = _setup()
        briefing_engine = MagicMock(spec=BriefingEngine)
        briefing_engine.generate_briefing = AsyncMock(return_value=Ok(BriefingResult(
            briefing="getUserById fetches a user by ID.",
            relevant_symbols=[{"name": "getUserById", "file": "src/users/queries.py"}],
            warnings=["Watch out for null returns"],
        )))

        result = await handle_brief(
            intent="understand getUserById",
            briefing_engine=briefing_engine,
            tracker=ctx["tracker"],
            store=ctx["store"],
        )

        assert result["briefing"] == "getUserById fetches a user by ID."
        assert len(result["relevant_symbols"]) == 1
        assert len(result["warnings"]) == 1

    @pytest.mark.asyncio
    async def test_with_target_file(self) -> None:
        ctx = _setup()
        briefing_engine = MagicMock(spec=BriefingEngine)
        briefing_engine.generate_briefing = AsyncMock(return_value=Ok(BriefingResult(
            briefing="Info about users route.",
        )))

        result = await handle_brief(
            intent="add auth",
            briefing_engine=briefing_engine,
            tracker=ctx["tracker"],
            store=ctx["store"],
            target_file="src/routes/users.py",
        )

        briefing_engine.generate_briefing.assert_called_once_with(
            "add auth", "src/routes/users.py"
        )
        assert "briefing" in result


class TestHandleValidate:
    @pytest.mark.asyncio
    async def test_valid_code(self) -> None:
        ctx = _setup()
        orchestrator = MagicMock(spec=ValidationOrchestrator)
        orchestrator.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=True, errors=[], ai_used=False, latency_ms=5,
        )))

        result = await handle_validate(
            proposed_code="from users.queries import getUserById",
            file_path="src/routes/users.py",
            orchestrator=orchestrator,
            tracker=ctx["tracker"],
            store=ctx["store"],
        )

        assert result["valid"] is True
        assert result["errors"] == []
        assert result["ai_used"] is False

    @pytest.mark.asyncio
    async def test_invalid_imports(self) -> None:
        ctx = _setup()
        orchestrator = MagicMock(spec=ValidationOrchestrator)
        orchestrator.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=False,
            errors=[{
                "type": "wrong_module_path",
                "message": "hashPassword not found in auth/",
                "suggestion": {
                    "source": "deterministic",
                    "fix": "from utils.crypto import hashPassword",
                    "confidence": 0.95,
                    "reason": "found at different path",
                },
            }],
            ai_used=False,
            latency_ms=8,
        )))

        result = await handle_validate(
            proposed_code="from auth import hashPassword",
            file_path="src/routes/users.py",
            orchestrator=orchestrator,
            tracker=ctx["tracker"],
            store=ctx["store"],
        )

        assert result["valid"] is False
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_ai_fallback(self) -> None:
        ctx = _setup()
        orchestrator = MagicMock(spec=ValidationOrchestrator)
        orchestrator.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=False,
            errors=[{"type": "unknown_import", "message": "cannot resolve"}],
            ai_used=True,
            latency_ms=150,
        )))

        result = await handle_validate(
            proposed_code="import something_weird",
            file_path="src/foo.py",
            orchestrator=orchestrator,
            tracker=ctx["tracker"],
            store=ctx["store"],
        )

        assert result["ai_used"] is True


class TestHandleTrace:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        ctx = _setup()

        result = await handle_trace(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
        )

        assert result["symbol"]["name"] == "getUserById"
        assert result["symbol"]["file"] == "src/users/queries.py"
        assert len(result["callers"]) > 0
        assert isinstance(result["impact_radius"], int)

    @pytest.mark.asyncio
    async def test_unknown_symbol(self) -> None:
        ctx = _setup()

        result = await handle_trace(
            symbol="doesNotExist",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_direction_callers_only(self) -> None:
        ctx = _setup()

        result = await handle_trace(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            direction="callers",
        )

        assert len(result["callers"]) > 0
        assert result["callees"] == []

    @pytest.mark.asyncio
    async def test_direction_callees_only(self) -> None:
        ctx = _setup()

        result = await handle_trace(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            direction="callees",
        )

        assert result["callers"] == []


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_correct_counts(self) -> None:
        ctx = _setup()

        result = await handle_status(
            store=ctx["store"],
            tracker=ctx["tracker"],
        )

        assert result["indexed"] is True
        assert result["symbols_count"] == 3
        assert result["files_count"] == 3
        assert result["refs_count"] == 2
        assert "python" in result["languages"]
        assert isinstance(result["interventions"], dict)

    @pytest.mark.asyncio
    async def test_empty_store(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        tracker = InterventionTracker(store)

        result = await handle_status(store=store, tracker=tracker)

        assert result["indexed"] is False
        assert result["symbols_count"] == 0
        assert result["files_count"] == 0


class TestHandleDeadCode:
    @pytest.mark.asyncio
    async def test_finds_dead_symbols(self) -> None:
        ctx = _setup()
        # Symbols in _setup are exported with usage_count=0
        result = await handle_dead_code(store=ctx["store"], tracker=ctx["tracker"])

        assert "dead_symbols" in result
        assert result["total"] == 3  # all 3 symbols are exported, usage_count=0
        names = {s["name"] for s in result["dead_symbols"]}
        assert "getUserById" in names

    @pytest.mark.asyncio
    async def test_excludes_used_symbols(self) -> None:
        ctx = _setup()
        store: SymbolStore = ctx["store"]
        # Mark getUserById as used
        store.update_usage_count(ctx["sym1_id"], 5)

        result = await handle_dead_code(store=store, tracker=ctx["tracker"])

        names = {s["name"] for s in result["dead_symbols"]}
        assert "getUserById" not in names
        assert result["total"] == 2


class TestHandleUnusedPackages:
    @pytest.mark.asyncio
    async def test_finds_unused(self) -> None:
        ctx = _setup()
        store: SymbolStore = ctx["store"]
        store.insert_package("axios", "1.6.0", "npm")
        store.insert_package("express", "4.0.0", "npm")

        result = await handle_unused_packages(store=store, tracker=ctx["tracker"])

        assert result["total"] == 2
        names = {p["name"] for p in result["unused_packages"]}
        assert names == {"axios", "express"}

    @pytest.mark.asyncio
    async def test_excludes_used_packages(self) -> None:
        ctx = _setup()
        store: SymbolStore = ctx["store"]
        store.insert_package("express", "4.0.0", "npm")

        # Create a symbol named "express" and an import ref to it
        r = store.insert_symbol(
            name="express", kind="variable", language="typescript",
            file_path="node_modules/express/index.ts", line_number=1, end_line=1,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=int(time.time()),
        )
        assert isinstance(r, Ok)
        store.insert_ref(r.value, "src/index.ts", 1, "import")

        result = await handle_unused_packages(store=store, tracker=ctx["tracker"])
        assert result["total"] == 0


class TestHandleHotspots:
    @pytest.mark.asyncio
    async def test_returns_hotspots_ordered(self) -> None:
        ctx = _setup()
        store: SymbolStore = ctx["store"]
        # Set usage counts
        store.update_usage_count(ctx["sym1_id"], 10)
        store.update_usage_count(ctx["sym2_id"], 5)

        result = await handle_hotspots(store=store, tracker=ctx["tracker"])

        assert len(result["hotspots"]) == 2
        assert result["hotspots"][0]["name"] == "getUserById"
        assert result["hotspots"][0]["usage_count"] == 10
        assert result["hotspots"][1]["name"] == "NotFoundError"

    @pytest.mark.asyncio
    async def test_limit_parameter(self) -> None:
        ctx = _setup()
        store: SymbolStore = ctx["store"]
        store.update_usage_count(ctx["sym1_id"], 10)
        store.update_usage_count(ctx["sym2_id"], 5)

        result = await handle_hotspots(store=store, tracker=ctx["tracker"], limit=1)

        assert len(result["hotspots"]) == 1
        assert result["hotspots"][0]["name"] == "getUserById"


class TestHandleOrient:
    @pytest.mark.asyncio
    async def test_returns_structure(self) -> None:
        ctx = _setup()
        import tempfile
        tmpdir = tempfile.mkdtemp()
        # Create a pyproject.toml
        import os
        with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
            f.write("[project]\nname = 'test'\n[project.scripts]\nrun = 'test:main'\n")
        os.makedirs(os.path.join(tmpdir, "src"))
        os.makedirs(os.path.join(tmpdir, "tests"))

        risk_scorer = RiskScorer(ctx["store"])
        result = await handle_orient(
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            risk_scorer=risk_scorer,
            root_path=tmpdir,
        )

        assert "project" in result
        assert "structure" in result
        assert "entry_points" in result
        assert "top_modules" in result
        assert "risk_summary" in result
        assert "src" in result["structure"]["top_level_dirs"]
        assert "tests" in result["structure"]["test_dirs"]
        assert "pyproject.toml" in result["structure"]["config_files"]

    @pytest.mark.asyncio
    async def test_records_intervention(self) -> None:
        ctx = _setup()
        import tempfile
        tmpdir = tempfile.mkdtemp()
        risk_scorer = RiskScorer(ctx["store"])
        await handle_orient(
            store=ctx["store"], graph=ctx["graph"], tracker=ctx["tracker"],
            risk_scorer=risk_scorer, root_path=tmpdir,
        )
        summary = ctx["tracker"].get_session_summary()
        assert summary.tools_called.get("groundtruth_orient", 0) >= 1


class TestHandleCheckpoint:
    @pytest.mark.asyncio
    async def test_session_summary(self) -> None:
        ctx = _setup()
        tracker: InterventionTracker = ctx["tracker"]
        risk_scorer = RiskScorer(ctx["store"])

        # Simulate some tool calls
        tracker.record(tool="groundtruth_trace", phase="trace", outcome="valid")
        tracker.record(
            tool="groundtruth_validate", phase="validate", outcome="fixed_deterministic",
            file_path="src/users/queries.py", errors_found=2, errors_fixed=1,
        )

        result = await handle_checkpoint(
            store=ctx["store"], tracker=tracker, risk_scorer=risk_scorer,
        )

        assert result["session"]["total_calls"] >= 2
        assert result["session"]["errors_found"] >= 2
        assert isinstance(result["recommendations"], list)

    @pytest.mark.asyncio
    async def test_recommends_briefing_when_none_used(self) -> None:
        ctx = _setup()
        tracker: InterventionTracker = ctx["tracker"]
        risk_scorer = RiskScorer(ctx["store"])
        tracker.record(tool="groundtruth_trace", phase="trace", outcome="valid")

        result = await handle_checkpoint(
            store=ctx["store"], tracker=tracker, risk_scorer=risk_scorer,
        )

        recs = result["recommendations"]
        assert any("groundtruth_brief" in r for r in recs)


class TestHandleSymbols:
    @pytest.mark.asyncio
    async def test_returns_symbols(self) -> None:
        ctx = _setup()

        result = await handle_symbols(
            file_path="src/users/queries.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
        )

        assert result["file_path"] == "src/users/queries.py"
        assert result["symbol_count"] >= 1
        names = [s["name"] for s in result["symbols"]]
        assert "getUserById" in names

    @pytest.mark.asyncio
    async def test_empty_file(self) -> None:
        ctx = _setup()

        result = await handle_symbols(
            file_path="nonexistent/file.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
        )

        assert result["symbol_count"] == 0
        assert result["symbols"] == []

    @pytest.mark.asyncio
    async def test_imports_and_importers(self) -> None:
        ctx = _setup()

        result = await handle_symbols(
            file_path="src/users/queries.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
        )

        # src/routes/users.py imports getUserById from src/users/queries.py
        assert isinstance(result["imported_by"], list)
        assert isinstance(result["imports_from"], list)


class TestHandleContext:
    @pytest.mark.asyncio
    async def test_finds_usages(self) -> None:
        ctx = _setup()
        import tempfile
        import os
        tmpdir = tempfile.mkdtemp()

        # Create a file with content
        os.makedirs(os.path.join(tmpdir, "src", "routes"), exist_ok=True)
        with open(os.path.join(tmpdir, "src", "routes", "users.py"), "w") as f:
            f.write("# line 1\n# line 2\nfrom users import getUserById\n# line 4\n")

        result = await handle_context(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=tmpdir,
        )

        assert result["symbol"]["name"] == "getUserById"
        assert result["total_usages"] >= 1
        # Check usages have file and line
        for u in result["usages"]:
            assert "file" in u
            assert "line" in u

    @pytest.mark.asyncio
    async def test_context_snippet_with_marker(self) -> None:
        ctx = _setup()
        import tempfile
        import os
        tmpdir = tempfile.mkdtemp()

        os.makedirs(os.path.join(tmpdir, "src", "routes"), exist_ok=True)
        with open(os.path.join(tmpdir, "src", "routes", "users.py"), "w") as f:
            f.write("line1\nline2\ngetUserById(42)\nline4\nline5\n")

        result = await handle_context(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=tmpdir,
        )

        # Find usage in users.py at line 3
        for u in result["usages"]:
            if u.get("file") == "src/routes/users.py" and u.get("line") == 3:
                assert ">>>" in u.get("context", "")
                break

    @pytest.mark.asyncio
    async def test_unknown_symbol(self) -> None:
        ctx = _setup()
        import tempfile
        tmpdir = tempfile.mkdtemp()

        result = await handle_context(
            symbol="nonExistent",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=tmpdir,
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_file_graceful(self) -> None:
        ctx = _setup()
        import tempfile
        tmpdir = tempfile.mkdtemp()

        result = await handle_context(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=tmpdir,
        )

        # Should still return usages, just without context snippets
        assert result["symbol"]["name"] == "getUserById"
        assert result["total_usages"] >= 1
