from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth import GroundTruth


def test_crewai_tools_smoke(graph_db_path: Path) -> None:
    pytest.importorskip("crewai")
    pytest.importorskip("pydantic")
    from groundtruth.integrations.crewai import as_crewai_tools

    gt = GroundTruth(str(graph_db_path))
    try:
        briefing_tool, check_tool, context_tool = as_crewai_tools(gt)
        names = {briefing_tool.name, check_tool.name, context_tool.name}
        assert names == {"gt_briefing", "gt_check", "gt_context"}
    finally:
        gt.close()
