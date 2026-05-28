"""Invariant tests for test file discovery via naming convention.

TEST-INV-1: If test_<stem>.py exists in graph.db for <stem>.py,
            the file-grep fallback must find it even without graph edges.

Research:
- TCTracer ICSE 2020: naming convention signal (test_foo → foo, weight 2.0)
- RepoGraph ICLR 2025: test functions discoverable via is_test flag
"""
import os
import sqlite3
import pytest


def _create_test_graph(db_path, source_files, test_files):
    """Create graph.db with source and test files but NO edges between them."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL DEFAULT 'python',
            parent_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT
        )
    """)
    for name, fpath in source_files:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, is_exported, is_test) "
            "VALUES ('Function', ?, ?, 1, 0)", (name, fpath)
        )
    for name, fpath in test_files:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, is_exported, is_test) "
            "VALUES ('Function', ?, ?, 0, 1)", (name, fpath)
        )
    conn.commit()
    conn.close()


@pytest.fixture
def flexget_graph(tmp_path):
    """flexget: qbittorrent.py source, test_qbittorrent.py test, NO edges."""
    db = tmp_path / "graph.db"
    _create_test_graph(
        db,
        source_files=[
            ("add_entries", "flexget/plugins/clients/qbittorrent.py"),
            ("connect", "flexget/plugins/clients/qbittorrent.py"),
        ],
        test_files=[
            ("test_ratio_limit", "flexget/tests/test_qbittorrent.py"),
            ("test_connect", "flexget/tests/test_qbittorrent.py"),
        ],
    )
    # Create the test file on disk so discovery validates it exists
    test_dir = tmp_path / "flexget" / "tests"
    test_dir.mkdir(parents=True)
    (test_dir / "test_qbittorrent.py").write_text(
        "def test_ratio_limit():\n    assert ratio_limit == '1.5'\n"
        "def test_connect():\n    assert connected is True\n"
    )
    return db, str(tmp_path)


@pytest.fixture
def pypsa_graph(tmp_path):
    """pypsa: expressions.py source, test_statistics.py test, NO edges."""
    db = tmp_path / "graph.db"
    _create_test_graph(
        db,
        source_files=[
            ("expanded_capacity", "pypsa/statistics/expressions.py"),
            ("optimal_capacity", "pypsa/statistics/expressions.py"),
        ],
        test_files=[
            ("test_expanded_capacity", "test/test_statistics.py"),
            ("test_optimal_capacity", "test/test_statistics.py"),
        ],
    )
    test_dir = tmp_path / "test"
    test_dir.mkdir(parents=True)
    (test_dir / "test_statistics.py").write_text(
        "def test_expanded_capacity():\n    assert len(result) > 0\n"
    )
    return db, str(tmp_path)


@pytest.fixture
def no_match_graph(tmp_path):
    """No naming convention match — test file has unrelated name."""
    db = tmp_path / "graph.db"
    _create_test_graph(
        db,
        source_files=[("my_func", "src/core/auth.py")],
        test_files=[("test_integration", "tests/test_integration.py")],
    )
    test_dir = tmp_path / "tests"
    test_dir.mkdir(parents=True)
    (test_dir / "test_integration.py").write_text(
        "def test_integration():\n    assert True\n"
    )
    return db, str(tmp_path)


class TestNamingConventionDiscovery:
    """TEST-INV-1: Naming convention finds test files without graph edges."""

    def test_flexget_test_qbittorrent_found(self, flexget_graph):
        from groundtruth.hooks.post_edit import _discover_test_files_by_convention
        db, repo = flexget_graph
        files = _discover_test_files_by_convention(
            str(db), "flexget/plugins/clients/qbittorrent.py", repo
        )
        assert any("test_qbittorrent" in f for f in files)

    def test_pypsa_test_statistics_not_found_by_stem(self, pypsa_graph):
        """expressions.py → test_expressions.py (doesn't exist), NOT test_statistics.py."""
        from groundtruth.hooks.post_edit import _discover_test_files_by_convention
        db, repo = pypsa_graph
        files = _discover_test_files_by_convention(
            str(db), "pypsa/statistics/expressions.py", repo
        )
        # test_statistics doesn't match test_expressions pattern
        assert not any("test_statistics" in f for f in files)

    def test_no_match_returns_empty(self, no_match_graph):
        from groundtruth.hooks.post_edit import _discover_test_files_by_convention
        db, repo = no_match_graph
        files = _discover_test_files_by_convention(
            str(db), "src/core/auth.py", repo
        )
        # test_integration doesn't match test_auth
        assert not any("test_integration" in f for f in files)

    def test_exact_stem_match(self, flexget_graph):
        from groundtruth.hooks.post_edit import _discover_test_files_by_convention
        db, repo = flexget_graph
        files = _discover_test_files_by_convention(
            str(db), "flexget/plugins/clients/qbittorrent.py", repo
        )
        # Should find test_qbittorrent.py by stem match
        assert len(files) >= 1


class TestFileGrepFallbackWithConvention:
    """TEST-INV-1: File-grep fallback uses convention when graph edges are empty."""

    def test_flexget_finds_assertions_without_edges(self, flexget_graph):
        from groundtruth.hooks.post_edit import _get_test_assertions_from_file
        db, repo = flexget_graph
        assertions = _get_test_assertions_from_file(
            str(db), "flexget/plugins/clients/qbittorrent.py",
            "ratio_limit", repo
        )
        assert len(assertions) >= 1
        assert any("ratio_limit" in a for a in assertions)
