"""Tests for handle_patterns tool handler."""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.tools import handle_patterns
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok


def _setup() -> dict[str, Any]:
    """Create store with sibling files and matching source on disk."""
    store = SymbolStore(":memory:")
    store.initialize()
    now = int(time.time())
    root = tempfile.mkdtemp()

    # Create 3 sibling files in src/routes/
    files = [
        ("src/routes/users.py", 5),
        ("src/routes/admin.py", 3),
        ("src/routes/auth.py", 2),
    ]
    for fp, usage in files:
        r = store.insert_symbol(
            name=f"handle_{os.path.basename(fp).replace('.py', '')}",
            kind="function",
            language="python",
            file_path=fp,
            line_number=1,
            end_line=20,
            is_exported=True,
            signature=None,
            params=None,
            return_type=None,
            documentation=None,
            last_indexed_at=now,
        )
        assert isinstance(r, Ok)
        store.update_usage_count(r.value, usage)

    # Create source files with patterns
    os.makedirs(os.path.join(root, "src", "routes"), exist_ok=True)
    for fp, _ in files:
        full = os.path.join(root, fp)
        with open(full, "w") as f:
            f.write(
                "import logging\n"
                "logger = logging.getLogger(__name__)\n"
                "\n"
                "def handle():\n"
                "    try:\n"
                "        logger.info('handling')\n"
                "        result = process()\n"
                "    except Exception as e:\n"
                "        logger.error(f'failed: {e}')\n"
                "        raise\n"
            )

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)

    return {
        "store": store,
        "graph": graph,
        "tracker": tracker,
        "root_path": root,
    }


class TestHandlePatterns:
    @pytest.mark.asyncio
    async def test_detects_error_handling(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        pattern_names = [p["pattern_name"] for p in result["patterns_detected"]]
        assert "error_handling" in pattern_names

    @pytest.mark.asyncio
    async def test_detects_logging(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        pattern_names = [p["pattern_name"] for p in result["patterns_detected"]]
        assert "logging" in pattern_names

    @pytest.mark.asyncio
    async def test_frequency_format(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        for p in result["patterns_detected"]:
            assert "/" in p["frequency"]  # e.g. "2/2 files"

    @pytest.mark.asyncio
    async def test_example_extraction(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        for p in result["patterns_detected"]:
            assert isinstance(p["example"], str)

    @pytest.mark.asyncio
    async def test_empty_siblings(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        tracker = InterventionTracker(store)
        result = await handle_patterns(
            file_path="src/isolated/lonely.py",
            store=store,
            tracker=tracker,
            root_path=tempfile.mkdtemp(),
        )
        assert result["sibling_files_analyzed"] == 0
        assert result["patterns_detected"] == []

    @pytest.mark.asyncio
    async def test_directory_field(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert result["directory"] == "src/routes"

    @pytest.mark.asyncio
    async def test_sibling_count(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        # 2 siblings (admin.py, auth.py — excluding users.py itself)
        assert result["sibling_files_analyzed"] == 2

    @pytest.mark.asyncio
    async def test_reasoning_guidance_with_patterns(self) -> None:
        ctx = _setup()
        result = await handle_patterns(
            file_path="src/routes/users.py",
            store=ctx["store"],
            tracker=ctx["tracker"],
            root_path=ctx["root_path"],
        )
        assert "reasoning_guidance" in result
        assert len(result["reasoning_guidance"]) > 0

    @pytest.mark.asyncio
    async def test_reasoning_guidance_no_patterns(self) -> None:
        store = SymbolStore(":memory:")
        store.initialize()
        tracker = InterventionTracker(store)
        result = await handle_patterns(
            file_path="src/isolated/lonely.py",
            store=store,
            tracker=tracker,
            root_path=tempfile.mkdtemp(),
        )
        assert "reasoning_guidance" in result
        assert "No strong conventions" in result["reasoning_guidance"]

    @pytest.mark.asyncio
    async def test_threshold_filtering(self) -> None:
        """Patterns below 60% threshold should not appear."""
        store = SymbolStore(":memory:")
        store.initialize()
        now = int(time.time())
        root = tempfile.mkdtemp()

        # 3 files but only 1 has try/except -> 33% < 60%
        for i, name in enumerate(["a.py", "b.py", "c.py"]):
            r = store.insert_symbol(
                name=f"func_{name}",
                kind="function",
                language="python",
                file_path=f"src/{name}",
                line_number=1,
                end_line=5,
                is_exported=True,
                signature=None,
                params=None,
                return_type=None,
                documentation=None,
                last_indexed_at=now,
            )
            assert isinstance(r, Ok)

        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        # Only a.py has try/except
        with open(os.path.join(root, "src", "a.py"), "w") as f:
            f.write("try:\n    pass\nexcept:\n    pass\n")
        with open(os.path.join(root, "src", "b.py"), "w") as f:
            f.write("x = 1\ny = 2\n")
        with open(os.path.join(root, "src", "c.py"), "w") as f:
            f.write("z = 3\nw = 4\n")

        tracker = InterventionTracker(store)
        result = await handle_patterns(
            file_path="src/a.py",
            store=store,
            tracker=tracker,
            root_path=root,
        )
        pattern_names = [p["pattern_name"] for p in result["patterns_detected"]]
        assert "error_handling" not in pattern_names
