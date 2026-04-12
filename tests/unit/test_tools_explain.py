"""Tests for handle_explain tool handler."""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools import handle_explain
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok


def _setup(tmpdir: str | None = None) -> dict[str, Any]:
    """Create a populated store with source files on disk."""
    store = SymbolStore(":memory:")
    store.initialize()
    now = int(time.time())

    root = tmpdir or tempfile.mkdtemp()

    r1 = store.insert_symbol(
        name="process_data",
        kind="function",
        language="python",
        file_path="src/core/processor.py",
        line_number=5,
        end_line=25,
        is_exported=True,
        signature="(data: list[dict]) -> Result",
        params=None,
        return_type="Result",
        documentation="Process incoming data records.",
        last_indexed_at=now,
    )
    assert isinstance(r1, Ok)
    sym1_id = r1.value

    r2 = store.insert_symbol(
        name="save_result",
        kind="function",
        language="python",
        file_path="src/core/storage.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(result: Result) -> None",
        params=None,
        return_type="None",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r2, Ok)

    # References
    store.insert_ref(sym1_id, "src/routes/api.py", 10, "import")
    store.insert_ref(sym1_id, "src/routes/api.py", 25, "call")
    store.insert_ref(sym1_id, "tests/test_processor.py", 5, "import")

    # Create source file on disk
    os.makedirs(os.path.join(root, "src", "core"), exist_ok=True)
    with open(os.path.join(root, "src", "core", "processor.py"), "w") as f:
        f.write(
            "import os\n"
            "from .storage import save_result\n"
            "\n"
            "\n"
            "def process_data(data: list[dict]) -> Result:\n"
            "    try:\n"
            "        result = transform(data)\n"
            "        save_result(result)\n"
            "        self.state = 'done'\n"
            "        return result\n"
            "    except ValueError:\n"
            "        raise ProcessingError('bad data')\n"
        )

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    return {
        "store": store,
        "graph": graph,
        "tracker": tracker,
        "root_path": root,
        "sym1_id": sym1_id,
    }


class TestHandleExplain:
    @pytest.mark.asyncio
    async def test_returns_symbol_info(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert result["symbol"]["name"] == "process_data"
        assert result["symbol"]["kind"] == "function"
        assert result["symbol"]["signature"] == "(data: list[dict]) -> Result"

    @pytest.mark.asyncio
    async def test_returns_source_code(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "def process_data" in result["source_code"]

    @pytest.mark.asyncio
    async def test_callers_returned(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert len(result["called_by"]) >= 1
        files = [c["file"] for c in result["called_by"]]
        assert "src/routes/api.py" in files

    @pytest.mark.asyncio
    async def test_side_effects_detected(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert len(result["side_effects_detected"]) >= 1
        assert any("state mutation" in s for s in result["side_effects_detected"])

    @pytest.mark.asyncio
    async def test_error_handling_detected(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert result["error_handling"]["has_try_catch"] is True
        assert result["error_handling"]["raises_errors"] is True

    @pytest.mark.asyncio
    async def test_complexity_structure(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "lines" in result["complexity"]
        assert "callers" in result["complexity"]
        assert result["complexity"]["callers"] >= 1

    @pytest.mark.asyncio
    async def test_reasoning_guidance_present(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result
        assert len(result["reasoning_guidance"]) > 0
        assert "side effect" in result["reasoning_guidance"].lower()

    @pytest.mark.asyncio
    async def test_unknown_symbol(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="nonexistent",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_file_returns_empty_source(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="save_result",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        # save_result's file doesn't exist on disk
        assert result["source_code"] == ""

    @pytest.mark.asyncio
    async def test_file_path_filter(self) -> None:
        ctx = _setup()
        result = await handle_explain(
            symbol="process_data",
            store=ctx["store"],
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
            file_path="src/core/processor.py",
        )
        assert result["symbol"]["file"] == "src/core/processor.py"

    @pytest.mark.asyncio
    async def test_ambiguous_symbol_requires_file_scope(self) -> None:
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

        result = await handle_explain(
            symbol="dup",
            store=store,
            graph=ctx["graph"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )

        assert "error" in result
        assert "ambiguous" in result["error"].lower()
