"""Tests for CLI commands and output rendering."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from groundtruth.analysis.risk_scorer import RiskScore
from groundtruth.cli.output import classify_risk, render_risk_summary, render_status_json
from groundtruth.index.store import SymbolStore


def _populate_store(store: SymbolStore) -> None:
    """Insert sample data into an in-memory store for testing."""
    now = int(time.time())
    for i in range(5):
        store.insert_symbol(
            name=f"func_{i}",
            kind="function",
            language="python",
            file_path=f"src/mod_{i}.py",
            line_number=i * 10,
            end_line=i * 10 + 5,
            is_exported=True,
            signature=f"(x: int) -> int",
            params=json.dumps([{"name": "x", "type": "int"}]),
            return_type="int",
            documentation=f"Function {i}",
            last_indexed_at=now,
        )
    # Add a ref
    store.insert_ref(
        symbol_id=1, referenced_in_file="src/mod_1.py",
        referenced_at_line=5, reference_type="call",
    )
    # Add a package
    store.insert_package(name="requests", version="2.31.0", package_manager="pip")


def test_detect_languages(tmp_path: object) -> None:
    """Temp dir with .py/.ts files — verify language detection from extensions."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        open(os.path.join(tmpdir, "app.py"), "w").close()
        open(os.path.join(tmpdir, "index.ts"), "w").close()
        os.makedirs(os.path.join(tmpdir, "node_modules"), exist_ok=True)
        open(os.path.join(tmpdir, "node_modules", "pkg.js"), "w").close()
        os.makedirs(os.path.join(tmpdir, "venv"), exist_ok=True)
        open(os.path.join(tmpdir, "venv", "lib.py"), "w").close()

        # Walk like the indexer does, respecting IGNORE_DIRS
        from groundtruth.index.indexer import IGNORE_DIRS

        extensions: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(tmpdir):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for f in filenames:
                ext = os.path.splitext(f)[1]
                if ext:
                    extensions.add(ext)

        assert ".py" in extensions
        assert ".ts" in extensions
        assert ".js" not in extensions  # node_modules skipped

        # Empty dir
        with tempfile.TemporaryDirectory() as empty_dir:
            found: set[str] = set()
            for _dirpath, _dirnames, filenames in os.walk(empty_dir):
                for f in filenames:
                    ext = os.path.splitext(f)[1]
                    if ext:
                        found.add(ext)
            assert len(found) == 0


def test_print_summary_formatting(in_memory_store: SymbolStore) -> None:
    """Populate store, render summary with is_tty=False, check formatting."""
    _populate_store(in_memory_store)

    stats_result = in_memory_store.get_stats()
    assert not isinstance(stats_result, type(None))
    stats = stats_result.value  # type: ignore[union-attr]

    risk_scores = [
        RiskScore(file_path="src/mod_0.py", overall_risk=0.1, factors={"naming_ambiguity": 0.1}),
        RiskScore(file_path="src/mod_1.py", overall_risk=0.4, factors={"import_depth": 0.4}),
        RiskScore(file_path="src/mod_2.py", overall_risk=0.8, factors={"convention_variance": 0.8}),
    ]

    output = render_risk_summary(
        project_name="test-project",
        stats=stats,
        risk_scores=risk_scores,
        dead_code_count=2,
        unused_packages_count=1,
        packages_count=3,
        elapsed_seconds=1.5,
        command="index",
        is_tty=False,
    )

    # Project name present
    assert "test-project" in output
    # Risk label present
    assert any(label in output for label in ("LOW", "MODERATE", "HIGH", "CRITICAL"))
    # No ANSI escape codes
    assert "\033[" not in output
    # Numbers are present
    assert "5" in output  # symbols_count
    # Elapsed time shown
    assert "1.5s" in output


def test_print_summary_json(in_memory_store: SymbolStore) -> None:
    """Render JSON output and validate structure."""
    _populate_store(in_memory_store)

    stats_result = in_memory_store.get_stats()
    stats = stats_result.value  # type: ignore[union-attr]

    risk_scores = [
        RiskScore(file_path="src/mod_0.py", overall_risk=0.2, factors={"naming_ambiguity": 0.2}),
        RiskScore(file_path="src/mod_1.py", overall_risk=0.6, factors={"import_depth": 0.6}),
    ]

    output = render_status_json(
        project_name="test-project",
        stats=stats,
        risk_scores=risk_scores,
        dead_code_count=3,
        unused_packages_count=1,
        packages_count=5,
    )

    data = json.loads(output)
    assert data["project"] == "test-project"
    assert "files" in data
    assert "symbols" in data
    assert "references" in data
    assert "risk_score" in data
    assert "risk_label" in data
    assert "risk_distribution" in data
    assert "hotspots" in data
    assert data["dead_code_count"] == 3
    assert data["unused_packages_count"] == 1

    # risk_distribution has expected keys
    dist = data["risk_distribution"]
    assert "low" in dist
    assert "moderate" in dist
    assert "high" in dist
    assert "critical" in dist


def test_status_no_index(tmp_path: object) -> None:
    """Loading store from a dir with no db file should exit 1."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        from groundtruth.cli.commands import _load_store

        with pytest.raises(SystemExit) as exc_info:
            _load_store(tmpdir)
        assert exc_info.value.code == 1


def test_index_force_flag(tmp_path: object) -> None:
    """With --force, existing db file should be removed before re-indexing."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        gt_dir = os.path.join(tmpdir, ".groundtruth")
        os.makedirs(gt_dir)
        db_file = os.path.join(gt_dir, "index.db")
        # Create dummy db
        with open(db_file, "w") as f:
            f.write("dummy")

        assert os.path.exists(db_file)

        from groundtruth.utils.result import Ok

        with (
            patch(
                "groundtruth.index.indexer.Indexer"
            ) as MockIndexer,
            patch(
                "groundtruth.lsp.manager.LSPManager"
            ) as MockLSPManager,
        ):
            mock_indexer_instance = MockIndexer.return_value
            mock_indexer_instance.index_project = AsyncMock(return_value=Ok(5))
            mock_lsp_instance = MockLSPManager.return_value
            mock_lsp_instance.shutdown_all = AsyncMock()

            from groundtruth.cli.commands import index_cmd

            # Should not raise — force removes old file
            index_cmd(tmpdir, force=True)


def test_index_creates_groundtruth_dir() -> None:
    """Indexing on a path with no .groundtruth/ should create the directory."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        gt_dir = os.path.join(tmpdir, ".groundtruth")
        assert not os.path.exists(gt_dir)

        from groundtruth.utils.result import Ok

        with (
            patch(
                "groundtruth.index.indexer.Indexer"
            ) as MockIndexer,
            patch(
                "groundtruth.lsp.manager.LSPManager"
            ) as MockLSPManager,
        ):
            mock_indexer_instance = MockIndexer.return_value
            mock_indexer_instance.index_project = AsyncMock(return_value=Ok(3))
            mock_lsp_instance = MockLSPManager.return_value
            mock_lsp_instance.shutdown_all = AsyncMock()

            from groundtruth.cli.commands import index_cmd

            index_cmd(tmpdir, force=True)

        assert os.path.isdir(gt_dir)


def test_version_flag() -> None:
    """--version flag should cause SystemExit with code 0."""
    import argparse

    from groundtruth import __version__
    from groundtruth.main import cli

    with (
        patch("sys.argv", ["groundtruth", "--version"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        cli()
    assert exc_info.value.code == 0


def test_classify_risk() -> None:
    """Risk classification thresholds."""
    assert classify_risk(0) == "LOW"
    assert classify_risk(25) == "LOW"
    assert classify_risk(26) == "MODERATE"
    assert classify_risk(50) == "MODERATE"
    assert classify_risk(51) == "HIGH"
    assert classify_risk(75) == "HIGH"
    assert classify_risk(76) == "CRITICAL"
    assert classify_risk(100) == "CRITICAL"


def test_get_all_files(in_memory_store: SymbolStore) -> None:
    """Test the new get_all_files() method."""
    _populate_store(in_memory_store)
    result = in_memory_store.get_all_files()
    assert not isinstance(result, type(None))
    files = result.value  # type: ignore[union-attr]
    assert len(files) == 5
    assert "src/mod_0.py" in files
