"""BUG-C4 + Patch 2: post_view.py correctness tests."""
import os
import sqlite3
import tempfile


def test_graph_navigation_error_goes_to_stdout(capsys, tmp_path):
    """Pre-fix: error goes to stderr (invisible inside container).
    Post-fix: error goes to stdout (captured by wrapper's _run_internal)."""
    from groundtruth.hooks.post_view import graph_navigation

    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_text("NOT A SQLITE DATABASE")

    lines, count = graph_navigation("fake/file.py", str(corrupt_db))
    captured = capsys.readouterr()

    assert "[GT_META] graph_navigation_error" in captured.out, (
        "BUG-C4: graph_navigation_error goes to stderr, not stdout"
    )
    assert "[GT_META] graph_navigation_error" not in captured.err
    assert lines == []
    assert count == 0


def test_test_file_targets_returns_source_connections(tmp_path):
    """Patch 2: _test_file_targets returns source functions called by test file."""
    from groundtruth.hooks.post_view import _test_file_targets

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.8, metadata TEXT
        );
        INSERT INTO nodes VALUES (1, 'Function', 'test_install_order', NULL,
            'test/test_build_order.py', 10, 30, NULL, NULL, 0, 1, 'python', NULL);
        INSERT INTO nodes VALUES (2, 'Method', 'install_order', NULL,
            'conans/client/graph/install_graph.py', 50, 80, NULL, NULL, 1, 0, 'python', NULL);
        INSERT INTO edges VALUES (1, 1, 2, 'CALLS', 15, 'test/test_build_order.py',
            'import', 0.9, NULL);
    """)
    conn.close()

    targets = _test_file_targets(db_path, "test/test_build_order.py")
    assert len(targets) >= 1
    assert any("install_order" in t and "Calls into:" in t for t in targets)


def test_test_file_targets_empty_on_no_edges(tmp_path):
    """Returns empty list when no source functions are connected."""
    from groundtruth.hooks.post_view import _test_file_targets

    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.8, metadata TEXT
        );
    """)
    conn.close()

    targets = _test_file_targets(db_path, "test/test_nothing.py")
    assert targets == []
