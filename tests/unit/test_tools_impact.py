"""Tests for handle_impact tool handler."""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools import handle_impact
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok


def _setup() -> dict[str, Any]:
    """Create a populated store for impact testing."""
    store = SymbolStore(":memory:")
    store.initialize()
    now = int(time.time())

    root = tempfile.mkdtemp()

    r1 = store.insert_symbol(
        name="getUserById",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=10,
        end_line=20,
        is_exported=True,
        signature="(user_id: int) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r1, Ok)
    sym1_id = r1.value
    store.update_usage_count(sym1_id, 5)

    r2 = store.insert_symbol(
        name="handle_request",
        kind="function",
        language="python",
        file_path="src/routes/users.py",
        line_number=1,
        end_line=30,
        is_exported=True,
        signature="(request) -> Response",
        params=None,
        return_type="Response",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r2, Ok)
    store.update_usage_count(r2.value, 3)

    r3 = store.insert_symbol(
        name="test_user",
        kind="function",
        language="python",
        file_path="tests/test_users.py",
        line_number=1,
        end_line=10,
        is_exported=False,
        signature="() -> None",
        params=None,
        return_type="None",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r3, Ok)

    # Refs: routes and tests call getUserById
    store.insert_ref(sym1_id, "src/routes/users.py", 15, "call")
    store.insert_ref(sym1_id, "tests/test_users.py", 5, "call")

    # Create source files with usage lines
    os.makedirs(os.path.join(root, "src", "routes"), exist_ok=True)
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    with open(os.path.join(root, "src", "routes", "users.py"), "w") as f:
        lines = [""] * 20
        lines[14] = "    user = getUserById(request.user_id)"
        f.write("\n".join(lines))
    with open(os.path.join(root, "tests", "test_users.py"), "w") as f:
        lines = [""] * 10
        lines[4] = "    result = getUserById(user_id=1)"
        f.write("\n".join(lines))

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    return {
        "store": store,
        "graph": graph,
        "tracker": tracker,
        "root_path": root,
    }


class TestHandleImpact:
    @pytest.mark.asyncio
    async def test_returns_symbol_info(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert result["symbol"]["name"] == "getUserById"

    @pytest.mark.asyncio
    async def test_direct_callers(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert len(result["direct_callers"]) >= 2
        files = [c["file"] for c in result["direct_callers"]]
        assert "src/routes/users.py" in files

    @pytest.mark.asyncio
    async def test_positional_call_high_risk(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        # routes/users.py uses positional: getUserById(request.user_id)
        route_caller = next(
            (c for c in result["direct_callers"] if c["file"] == "src/routes/users.py"), None
        )
        if route_caller:
            assert route_caller["break_risk"] == "HIGH"
            assert route_caller["call_style"] == "positional"

    @pytest.mark.asyncio
    async def test_keyword_call_moderate_risk(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        # tests/test_users.py uses keyword: getUserById(user_id=1)
        test_caller = next(
            (c for c in result["direct_callers"] if c["file"] == "tests/test_users.py"), None
        )
        if test_caller:
            assert test_caller["break_risk"] == "MODERATE"
            assert test_caller["call_style"] == "keyword"

    @pytest.mark.asyncio
    async def test_impact_summary(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        summary = result["impact_summary"]
        assert summary["direct_files"] >= 2
        assert summary["total_files_at_risk"] >= 2
        assert summary["impact_level"] in ("LOW", "MODERATE", "HIGH")

    @pytest.mark.asyncio
    async def test_safe_unsafe_changes(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert len(result["safe_changes"]) > 0
        assert len(result["unsafe_changes"]) > 0
        assert any("optional" in s.lower() for s in result["safe_changes"])
        assert any("rename" in s.lower() for s in result["unsafe_changes"])

    @pytest.mark.asyncio
    async def test_reasoning_guidance(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result
        assert len(result["reasoning_guidance"]) > 0

    @pytest.mark.asyncio
    async def test_unknown_symbol(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="nonexistent",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_indirect_dependents(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert isinstance(result["indirect_dependents"], list)

    @pytest.mark.asyncio
    async def test_impact_level_thresholds(self) -> None:
        ctx = _setup()
        result = await handle_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        summary = result["impact_summary"]
        if summary["total_files_at_risk"] >= 5:
            assert summary["impact_level"] == "HIGH"
        elif summary["total_files_at_risk"] >= 2:
            assert summary["impact_level"] == "MODERATE"
        else:
            assert summary["impact_level"] == "LOW"

    @pytest.mark.asyncio
    async def test_ambiguous_symbol_abstains(self) -> None:
        ctx = _setup()
        store: SymbolStore = ctx["store"]
        now = int(time.time())
        store.insert_symbol(
            name="dup",
            kind="function",
            language="python",
            file_path="src/a.py",
            line_number=1,
            end_line=3,
            is_exported=True,
            signature="() -> int",
            params=None,
            return_type="int",
            documentation=None,
            last_indexed_at=now,
        )
        store.insert_symbol(
            name="dup",
            kind="function",
            language="python",
            file_path="src/b.py",
            line_number=1,
            end_line=3,
            is_exported=True,
            signature="() -> int",
            params=None,
            return_type="int",
            documentation=None,
            last_indexed_at=now,
        )

        result = await handle_impact(
            symbol="dup",
            store=store,
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )

        assert "error" in result
        assert "ambiguous" in result["error"].lower()
