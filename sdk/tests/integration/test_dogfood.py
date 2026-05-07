from __future__ import annotations

from pathlib import Path

from groundtruth import GroundTruth


def test_end_to_end_sdk_read_path(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        brief = gt.briefing("leaf")
        assert brief.callers
        impact = gt.check("src/b.py", diff=None)
        assert "symbols=" in impact.summary
        ctx = gt.context("top", direction="both", depth=2)
        assert ctx.evidence
        merged = gt.inject("TASK", ["top", "leaf"], fmt="markdown")
        assert "TASK" in merged
    finally:
        gt.close()
