from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth import GroundTruth
from groundtruth.integrations.langchain import as_langchain_tools
from groundtruth.integrations.openai import as_openai_tools


def test_openai_dispatch_smoke(graph_db_path: Path) -> None:
    pytest.importorskip("pydantic")
    gt = GroundTruth(str(graph_db_path))
    try:
        kit = as_openai_tools(gt)
        assert isinstance(kit["tools"], list)
        dispatch = kit["dispatch"]
        text = dispatch("gt_briefing", {"symbol": "mid"})
        assert "pkg.mid" in text or "mid" in text
        chk = dispatch("gt_check", {"path": "src/b.py", "diff": "@@"})
        assert "src/b.py" in chk
        ctx = dispatch("gt_context", {"symbol": "top", "direction": "callees", "depth": 2})
        assert ctx
    finally:
        gt.close()


def test_openai_dispatch_unknown_tool(graph_db_path: Path) -> None:
    pytest.importorskip("pydantic")
    gt = GroundTruth(str(graph_db_path))
    try:
        dispatch = as_openai_tools(gt)["dispatch"]
        with pytest.raises(ValueError):
            dispatch("unknown", {})
    finally:
        gt.close()


def test_langchain_tool_names(graph_db_path: Path) -> None:
    pytest.importorskip("langchain_core")
    gt = GroundTruth(str(graph_db_path))
    try:
        tools = as_langchain_tools(gt)
        names = sorted(getattr(t, "name", "") for t in tools)
        assert names == ["gt_briefing", "gt_check", "gt_context"]
    finally:
        gt.close()
