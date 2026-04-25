"""Tests for the three lifecycle surface handlers.

Uses in-memory SymbolStore so tests run without external dependencies.
"""

from __future__ import annotations

import time

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.utils.result import Ok


@pytest.fixture()
def store() -> SymbolStore:
    """Create an in-memory store with test symbols."""
    s = SymbolStore(":memory:")
    s.initialize()
    now = int(time.time())

    s.insert_symbol(
        name="encrypt",
        kind="function",
        language="python",
        file_path="src/model.py",
        line_number=42,
        end_line=60,
        is_exported=True,
        signature="(data: bytes) -> bytes",
        params="data: bytes",
        return_type="bytes",
        documentation=None,
        last_indexed_at=now,
    )
    s.insert_symbol(
        name="decrypt",
        kind="function",
        language="python",
        file_path="src/handlers.py",
        line_number=87,
        end_line=100,
        is_exported=True,
        signature="(ciphertext: bytes) -> bytes",
        params="ciphertext: bytes",
        return_type="bytes",
        documentation=None,
        last_indexed_at=now,
    )
    s.insert_symbol(
        name="test_encrypt",
        kind="function",
        language="python",
        file_path="tests/test_model.py",
        line_number=10,
        end_line=20,
        is_exported=False,
        signature="()",
        params=None,
        return_type=None,
        documentation=None,
        last_indexed_at=now,
    )
    return s


@pytest.fixture()
def graph(store: SymbolStore) -> ImportGraph:
    return ImportGraph(store)


# ── task_map ─────────────────────────────────────────────────────────────────


class TestTaskMapIdentifiers:
    def test_backtick_extraction(self) -> None:
        from groundtruth.mcp.endpoints.task_map import _extract_identifiers

        ids = _extract_identifiers("Fix `encrypt` to handle empty data")
        assert "encrypt" in ids

    def test_dotted_extraction(self) -> None:
        from groundtruth.mcp.endpoints.task_map import _extract_identifiers

        ids = _extract_identifiers("Bug in model.encrypt when called")
        assert "model.encrypt" in ids

    def test_function_call_extraction(self) -> None:
        from groundtruth.mcp.endpoints.task_map import _extract_identifiers

        ids = _extract_identifiers("when encrypt() is called with None")
        assert "encrypt" in ids

    def test_dedup(self) -> None:
        from groundtruth.mcp.endpoints.task_map import _extract_identifiers

        ids = _extract_identifiers("`encrypt` and encrypt() and encrypt")
        assert ids.count("encrypt") == 1


class TestTaskMapResolve:
    def test_resolve_known_symbol(self, store: SymbolStore) -> None:
        from groundtruth.mcp.endpoints.task_map import _resolve_targets

        targets = _resolve_targets(["encrypt"], store)
        assert len(targets) >= 1
        assert targets[0]["name"] == "encrypt"
        assert targets[0]["file"] == "src/model.py"

    def test_resolve_unknown_symbol(self, store: SymbolStore) -> None:
        from groundtruth.mcp.endpoints.task_map import _resolve_targets

        targets = _resolve_targets(["nonexistent_symbol"], store)
        assert targets == []


@pytest.mark.asyncio
class TestTaskMapHandler:
    async def test_basic(self, store: SymbolStore, graph: ImportGraph) -> None:
        from groundtruth.mcp.endpoints.task_map import handle_task_map

        result = await handle_task_map(
            issue_text="Fix `encrypt` to handle empty data",
            store=store,
            graph=graph,
            root_path="/tmp/fake",
        )
        assert "findings" in result
        assert "text" in result
        assert "targets" in result
        assert len(result["findings"]) >= 1
        assert result["text"].startswith('<gt-evidence surface="task_map">')

    async def test_empty_issue(self, store: SymbolStore, graph: ImportGraph) -> None:
        from groundtruth.mcp.endpoints.task_map import handle_task_map

        result = await handle_task_map(
            issue_text="",
            store=store,
            graph=graph,
            root_path="/tmp/fake",
        )
        assert result["findings"] == []
        assert result["text"] == ""

    async def test_novelty_filter(self, store: SymbolStore, graph: ImportGraph) -> None:
        from groundtruth.mcp.endpoints.task_map import handle_task_map

        nf = NoveltyFilter()
        r1 = await handle_task_map(
            issue_text="Fix `encrypt`",
            store=store,
            graph=graph,
            root_path="/tmp/fake",
            novelty_filter=nf,
        )
        assert len(r1["findings"]) >= 1
        r2 = await handle_task_map(
            issue_text="Fix `encrypt`",
            store=store,
            graph=graph,
            root_path="/tmp/fake",
            novelty_filter=nf,
        )
        assert len(r2["findings"]) == 0


# ── event_brief ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestEventBriefHandler:
    async def test_no_diff_returns_empty(self, store: SymbolStore, graph: ImportGraph) -> None:
        from groundtruth.mcp.endpoints.event_brief import handle_event_brief

        result = await handle_event_brief(
            file_path="src/model.py",
            store=store,
            graph=graph,
            root_path="/tmp/nonexistent",
        )
        assert result["text"] == ""
        assert result["findings"] == []


# ── review_patch ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestReviewPatchHandler:
    async def test_no_diff_returns_empty(self, store: SymbolStore, graph: ImportGraph) -> None:
        from groundtruth.mcp.endpoints.review_patch import handle_review_patch

        result = await handle_review_patch(
            store=store,
            graph=graph,
            root_path="/tmp/nonexistent",
        )
        assert result["text"] == ""
        assert result["findings"] == []
        assert result["modified_files"] == []


# ── Cross-surface novelty ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCrossSurfaceNovelty:
    async def test_findings_not_repeated_across_surfaces(
        self, store: SymbolStore, graph: ImportGraph
    ) -> None:
        from groundtruth.mcp.endpoints.task_map import handle_task_map

        nf = NoveltyFilter()

        r1 = await handle_task_map(
            issue_text="Fix `encrypt`",
            store=store,
            graph=graph,
            root_path="/tmp/fake",
            novelty_filter=nf,
        )
        initial_count = len(r1["findings"])
        assert initial_count >= 1

        r2 = await handle_task_map(
            issue_text="Fix `encrypt`",
            store=store,
            graph=graph,
            root_path="/tmp/fake",
            novelty_filter=nf,
        )
        assert len(r2["findings"]) < initial_count
