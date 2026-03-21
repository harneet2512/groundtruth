"""Tests for consolidated MCP tool handlers (core_tools.py)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools.core_tools import (
    _extract_files_from_diff,
    handle_consolidated_check,
    handle_consolidated_impact,
    handle_consolidated_orient,
    handle_consolidated_references,
    handle_consolidated_search,
)
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok
from groundtruth.validators.autocorrect import AutoCorrector


def _setup() -> dict[str, Any]:
    """Create a populated store and real graph/tracker."""
    store = SymbolStore(":memory:")
    store.initialize()

    now = int(time.time())

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
        documentation="Get a user by ID.",
        last_indexed_at=now,
    )
    assert isinstance(r1, Ok)
    sym1_id = r1.value

    r2 = store.insert_symbol(
        name="NotFoundError",
        kind="class",
        language="python",
        file_path="src/utils/errors.py",
        line_number=5,
        end_line=10,
        is_exported=True,
        signature=None,
        params=None,
        return_type=None,
        documentation="Not found error.",
        last_indexed_at=now,
    )
    assert isinstance(r2, Ok)
    sym2_id = r2.value

    r3 = store.insert_symbol(
        name="handle_users",
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
    assert isinstance(r3, Ok)

    # Insert refs
    store.insert_ref(sym1_id, "src/routes/users.py", 3, "import")
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


class TestConsolidatedImpact:
    @pytest.mark.asyncio
    async def test_returns_impact_and_obligations(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_impact(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )

        assert "symbol" in result
        assert result["symbol"]["name"] == "getUserById"
        assert "direct_callers" in result
        assert "obligations" in result
        assert "obligation_count" in result
        assert "scope_files" in result
        assert "impact_summary" in result
        assert "safe_changes" in result
        assert "unsafe_changes" in result

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_error(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_impact(
            symbol="nonExistent",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )
        assert "error" in result


class TestConsolidatedReferences:
    @pytest.mark.asyncio
    async def test_returns_references_and_edit_site(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_references(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )

        assert "symbol" in result
        assert result["symbol"]["name"] == "getUserById"
        assert result["symbol"]["file"] == "src/users/queries.py"
        assert "references" in result
        assert result["total_references"] >= 0
        assert "edit_site" in result
        assert "imports_from" in result
        assert "imported_by" in result
        assert "evidence" in result

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_error(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_references(
            symbol="nonExistent",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_max_results_limits_output(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_references(
            symbol="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
            max_results=1,
        )
        assert result["total_references"] <= 1


class TestConsolidatedCheck:
    @pytest.mark.asyncio
    async def test_clean_diff(self) -> None:
        ctx = _setup()
        autocorrector = AutoCorrector(
            ctx["store"], "/tmp/fake", benchmark_safe=False, graph=ctx["graph"]
        )

        diff = """\
--- a/src/users/queries.py
+++ b/src/users/queries.py
@@ -10,3 +10,5 @@
 def getUserById(user_id: int) -> User:
     pass
+    # added comment
"""
        result = await handle_consolidated_check(
            diff=diff,
            autocorrector=autocorrector,
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )

        assert "corrected_diff" in result
        assert "corrections" in result
        assert "obligations" in result
        assert "contradictions" in result
        assert "contradiction_count" in result
        # test_suggestion may or may not be present
        assert "test_suggestion" in result

    @pytest.mark.asyncio
    async def test_obligations_from_diff(self) -> None:
        ctx = _setup()
        autocorrector = AutoCorrector(
            ctx["store"], "/tmp/fake", benchmark_safe=False, graph=ctx["graph"]
        )

        diff = """\
--- a/src/users/queries.py
+++ b/src/users/queries.py
@@ -10,3 +10,5 @@
 def getUserById(user_id: int) -> User:
-    pass
+    return db.query(user_id)
"""
        result = await handle_consolidated_check(
            diff=diff,
            autocorrector=autocorrector,
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )

        assert "obligation_count" in result
        assert isinstance(result["obligation_count"], int)


class TestConsolidatedOrient:
    @pytest.mark.asyncio
    async def test_overview_mode(self) -> None:
        ctx = _setup()
        risk_scorer = RiskScorer(ctx["store"])

        result = await handle_consolidated_orient(
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            risk_scorer=risk_scorer,
            root_path="/tmp/fake",
            depth="overview",
        )

        assert "project" in result
        assert "structure" in result
        assert "index_health" in result
        assert "freshness_warnings" in result
        assert "conventions" in result
        # In overview mode, conventions should be empty (no path provided)
        assert result["conventions"] == []

    @pytest.mark.asyncio
    async def test_detailed_mode(self) -> None:
        ctx = _setup()
        risk_scorer = RiskScorer(ctx["store"])

        result = await handle_consolidated_orient(
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            risk_scorer=risk_scorer,
            root_path="/tmp/fake",
            depth="detailed",
        )

        assert "hotspots" in result
        # Hotspots should be populated in detailed mode
        assert isinstance(result["hotspots"], list)


class TestConsolidatedSearch:
    @pytest.mark.asyncio
    async def test_finds_matching_symbols(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_search(
            query="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )

        assert "matches" in result
        assert result["total_matches"] > 0
        names = [m["name"] for m in result["matches"]]
        assert "getUserById" in names

    @pytest.mark.asyncio
    async def test_no_results_for_unknown(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_search(
            query="zzz_nonexistent_zzz",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
        )

        assert result["total_matches"] == 0

    @pytest.mark.asyncio
    async def test_include_dead_code(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_search(
            query="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
            include_dead_code=True,
        )

        assert "dead_symbols" in result
        assert "dead_total" in result

    @pytest.mark.asyncio
    async def test_max_results(self) -> None:
        ctx = _setup()
        result = await handle_consolidated_search(
            query="getUserById",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path="/tmp/fake",
            max_results=1,
        )

        assert result["total_matches"] <= 1


class TestExtractFilesFromDiff:
    def test_extracts_file_paths(self) -> None:
        diff = """\
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,5 @@
 line1
--- a/src/bar.py
+++ b/src/bar.py
@@ -1,3 +1,5 @@
 line1
"""
        files = _extract_files_from_diff(diff)
        assert files == ["src/foo.py", "src/bar.py"]

    def test_handles_empty_diff(self) -> None:
        files = _extract_files_from_diff("")
        assert files == []

    def test_skips_dev_null(self) -> None:
        diff = """\
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1,3 @@
+content
"""
        files = _extract_files_from_diff(diff)
        assert files == ["new_file.py"]
