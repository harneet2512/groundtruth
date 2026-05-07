from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth import GroundTruth
from groundtruth.exceptions import GraphNotFoundError, SymbolNotFoundError


def test_graph_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(GraphNotFoundError):
        GroundTruth(str(missing))


def test_briefing_prefers_deterministic_callers(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        brief = gt.briefing("mid", max_results=10)
        assert "pkg.mid" in brief.symbol or brief.symbol == "pkg.mid"
        methods = [c.resolution_method for c in brief.callers]
        assert "import" in methods
        assert brief.evidence_text
    finally:
        gt.close()


def test_symbol_not_found(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        with pytest.raises(SymbolNotFoundError):
            gt.briefing("does_not_exist")
    finally:
        gt.close()


def test_check_file_level(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        impact = gt.check("src/b.py", diff="@@ example")
        assert "file=src/b.py" in impact.summary
        assert "example" in impact.summary
        assert impact.affected_symbols
    finally:
        gt.close()


def test_check_missing_file(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        with pytest.raises(FileNotFoundError):
            gt.check("src/missing.py")
    finally:
        gt.close()


def test_context_callers(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        # ``leaf`` inbound edges are ``name_match`` in the fixture; deterministic context skips them.
        ctx = gt.context("mid", direction="callers", depth=3)
        assert ctx.matches
        assert ctx.call_graph
        assert "mid" in ctx.evidence or "pkg.mid" in ctx.evidence
    finally:
        gt.close()


def test_context_scope_filter(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        ctx = gt.context("mid", direction="callers", scope="src/a.py", depth=3)
        assert all(m.file.startswith("src/a.py") for m in ctx.matches)
    finally:
        gt.close()


def test_inject_requires_symbols(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        with pytest.raises(ValueError):
            gt.inject("hello", [])
    finally:
        gt.close()


def test_inject_format_alias(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        out = gt.inject("prompt", ["top"], format="plain")
        assert "prompt" in out
        assert out.index("Briefing:") < out.index("prompt")
    finally:
        gt.close()
