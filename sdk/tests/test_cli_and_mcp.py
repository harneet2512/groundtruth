from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth import GroundTruth
from groundtruth.cli.main import cli_app
from groundtruth.mcp.server import gt_briefing, gt_check, gt_context


def test_mcp_helper_functions(graph_db_path: Path) -> None:
    gt = GroundTruth(str(graph_db_path))
    try:
        assert "mid" in gt_briefing(gt, symbol="mid")
        assert "src/b.py" in gt_check(gt, path="src/b.py", diff="@@")
        assert gt_context(gt, symbol="leaf", direction="callers", depth=3)
    finally:
        gt.close()


def test_cli_briefing_markdown(graph_db_path: Path) -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    if cli_app is None:  # pragma: no cover
        pytest.skip("Typer extra not available")

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["briefing", "top", "--db", str(graph_db_path), "--format", "markdown"],
    )
    assert result.exit_code == 0, result.output
    assert "top" in result.output or "pkg.top" in result.output


def test_cli_check(graph_db_path: Path) -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    if cli_app is None:  # pragma: no cover
        pytest.skip("Typer extra not available")

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["check", "src/c.py", "--db", str(graph_db_path), "--diff", "patch"],
    )
    assert result.exit_code == 0, result.output
    assert "patch" in result.output


def test_cli_context(graph_db_path: Path) -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    if cli_app is None:  # pragma: no cover
        pytest.skip("Typer extra not available")

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["context", "mid", "--db", str(graph_db_path), "--direction", "callers"],
    )
    assert result.exit_code == 0, result.output
