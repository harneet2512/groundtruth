"""Tests that every handler includes reasoning_guidance in its response."""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from groundtruth.ai.briefing import BriefingEngine, BriefingResult
from groundtruth.ai.task_parser import TaskParser
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
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok
from groundtruth.validators.orchestrator import ValidationOrchestrator, ValidationResult


def _setup() -> dict[str, Any]:
    """Create a populated store for reasoning_guidance tests."""
    store = SymbolStore(":memory:")
    store.initialize()
    now = int(time.time())
    root = tempfile.mkdtemp()

    r1 = store.insert_symbol(
        name="getUserById", kind="function", language="python",
        file_path="src/users/queries.py", line_number=10, end_line=20,
        is_exported=True, signature="(user_id: int) -> User",
        params=None, return_type="User", documentation="Get user.",
        last_indexed_at=now,
    )
    assert isinstance(r1, Ok)
    sym1_id = r1.value
    store.update_usage_count(sym1_id, 5)
    store.insert_ref(sym1_id, "src/routes/users.py", 3, "import")

    # Create source file
    os.makedirs(os.path.join(root, "src", "users"), exist_ok=True)
    with open(os.path.join(root, "src", "users", "queries.py"), "w") as f:
        f.write("def getUserById(user_id: int):\n    return db.query(user_id)\n")

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    return {
        "store": store, "graph": graph, "tracker": tracker,
        "root_path": root, "sym1_id": sym1_id,
    }


class TestReasoningGuidance:
    @pytest.mark.asyncio
    async def test_find_relevant_has_guidance(self) -> None:
        ctx = _setup()
        tp = MagicMock(spec=TaskParser)
        tp.parse = AsyncMock(return_value=Ok(["getUserById"]))
        result = await handle_find_relevant(
            description="fix getUserById", store=ctx["store"],
            graph=ctx["graph"], task_parser=tp, tracker=ctx["tracker"],
        )
        assert "reasoning_guidance" in result
        assert len(result["reasoning_guidance"]) > 0

    @pytest.mark.asyncio
    async def test_brief_has_guidance(self) -> None:
        ctx = _setup()
        be = MagicMock(spec=BriefingEngine)
        be.generate_briefing = AsyncMock(return_value=Ok(BriefingResult(
            briefing="Info.", relevant_symbols=[{"name": "getUserById"}], warnings=[],
        )))
        result = await handle_brief(
            intent="understand", briefing_engine=be,
            tracker=ctx["tracker"], store=ctx["store"],
        )
        assert "reasoning_guidance" in result
        assert len(result["reasoning_guidance"]) > 0

    @pytest.mark.asyncio
    async def test_validate_errors_has_guidance(self) -> None:
        ctx = _setup()
        orch = MagicMock(spec=ValidationOrchestrator)
        orch.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=False,
            errors=[{"type": "wrong_module_path", "message": "not found"}],
            ai_used=False, latency_ms=5,
        )))
        result = await handle_validate(
            proposed_code="import x", file_path="src/foo.py",
            orchestrator=orch, tracker=ctx["tracker"], store=ctx["store"],
        )
        assert "reasoning_guidance" in result
        assert "error" in result["reasoning_guidance"].lower()

    @pytest.mark.asyncio
    async def test_validate_valid_has_guidance(self) -> None:
        ctx = _setup()
        orch = MagicMock(spec=ValidationOrchestrator)
        orch.validate = AsyncMock(return_value=Ok(ValidationResult(
            valid=True, errors=[], ai_used=False, latency_ms=5,
        )))
        result = await handle_validate(
            proposed_code="x = 1", file_path="src/foo.py",
            orchestrator=orch, tracker=ctx["tracker"], store=ctx["store"],
        )
        assert "reasoning_guidance" in result
        assert "structural" in result["reasoning_guidance"].lower()

    @pytest.mark.asyncio
    async def test_trace_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_trace(
            symbol="getUserById", store=ctx["store"],
            graph=ctx["graph"], tracker=ctx["tracker"],
        )
        assert "reasoning_guidance" in result
        assert "caller" in result["reasoning_guidance"].lower()

    @pytest.mark.asyncio
    async def test_status_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_status(store=ctx["store"], tracker=ctx["tracker"])
        assert "reasoning_guidance" in result
        assert len(result["reasoning_guidance"]) > 0

    @pytest.mark.asyncio
    async def test_dead_code_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_dead_code(store=ctx["store"], tracker=ctx["tracker"])
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_unused_packages_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_unused_packages(store=ctx["store"], tracker=ctx["tracker"])
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_hotspots_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_hotspots(store=ctx["store"], tracker=ctx["tracker"])
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_orient_has_guidance(self) -> None:
        ctx = _setup()
        risk_scorer = RiskScorer(ctx["store"])
        result = await handle_orient(
            store=ctx["store"], graph=ctx["graph"], tracker=ctx["tracker"],
            risk_scorer=risk_scorer, root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_checkpoint_has_guidance(self) -> None:
        ctx = _setup()
        risk_scorer = RiskScorer(ctx["store"])
        result = await handle_checkpoint(
            store=ctx["store"], tracker=ctx["tracker"], risk_scorer=risk_scorer,
        )
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_symbols_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_symbols(
            file_path="src/users/queries.py", store=ctx["store"],
            tracker=ctx["tracker"],
        )
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_context_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_context(
            symbol="getUserById", store=ctx["store"],
            graph=ctx["graph"], tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_explain_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="getUserById", store=ctx["store"],
            graph=ctx["graph"], tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_impact_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById", store=ctx["store"],
            graph=ctx["graph"], tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result

    @pytest.mark.asyncio
    async def test_patterns_has_guidance(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/users/queries.py", store=ctx["store"],
            tracker=ctx["tracker"], root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result
