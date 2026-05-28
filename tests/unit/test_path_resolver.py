"""Tests for src/groundtruth/index/path_resolver.py — DOC_OF_HONOR §1.1."""
import os
import sqlite3
import tempfile
import pytest

from groundtruth.index.path_resolver import (
    resolve_to_stored_path,
    is_known,
    clear_cache,
)


@pytest.fixture
def graph_db():
    """Create a temp graph.db with a few nodes for resolution tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            language TEXT NOT NULL
        )
    """)
    nodes = [
        ("Function", "foo", "src/groundtruth/foo.py", "python"),
        ("Function", "bar", "src/groundtruth/sub/bar.py", "python"),
        ("Function", "baz", "tests/test_baz.py", "python"),
        ("Function", "unique", "lib/unique_basename.py", "python"),
        ("Function", "x", "a/dup.py", "python"),
        ("Function", "y", "b/dup.py", "python"),
    ]
    conn.executemany(
        "INSERT INTO nodes (label, name, file_path, language) VALUES (?,?,?,?)",
        nodes,
    )
    conn.commit()
    conn.close()
    clear_cache()
    yield path
    os.unlink(path)


def test_exact_match(graph_db):
    assert resolve_to_stored_path("src/groundtruth/foo.py", graph_db) == "src/groundtruth/foo.py"


def test_leading_slash_stripped(graph_db):
    assert resolve_to_stored_path("/src/groundtruth/foo.py", graph_db) == "src/groundtruth/foo.py"


def test_dot_slash_stripped(graph_db):
    assert resolve_to_stored_path("./src/groundtruth/foo.py", graph_db) == "src/groundtruth/foo.py"


def test_windows_separator(graph_db):
    assert resolve_to_stored_path("src\\groundtruth\\foo.py", graph_db) == "src/groundtruth/foo.py"


def test_workspace_prefix_stripped(graph_db):
    assert resolve_to_stored_path(
        "/workspace/myrepo/src/groundtruth/foo.py",
        graph_db,
        workspace_root="/workspace/myrepo",
    ) == "src/groundtruth/foo.py"


def test_container_workspace_prefix(graph_db):
    assert resolve_to_stored_path(
        "workspace/src/groundtruth/foo.py", graph_db
    ) == "src/groundtruth/foo.py"


def test_container_testbed_prefix(graph_db):
    assert resolve_to_stored_path(
        "testbed/src/groundtruth/foo.py", graph_db
    ) == "src/groundtruth/foo.py"


def test_instance_id_prefix(graph_db):
    assert resolve_to_stored_path(
        "kozea__weasyprint-2300/src/groundtruth/foo.py", graph_db
    ) == "src/groundtruth/foo.py"


def test_basename_unique_resolves(graph_db):
    assert resolve_to_stored_path("unique_basename.py", graph_db) == "lib/unique_basename.py"


def test_basename_ambiguous_returns_none(graph_db):
    assert resolve_to_stored_path("dup.py", graph_db) is None


def test_unknown_file_returns_none(graph_db):
    assert resolve_to_stored_path("does/not/exist.py", graph_db) is None


def test_empty_path_returns_none(graph_db):
    assert resolve_to_stored_path("", graph_db) is None


def test_empty_db_path_returns_none():
    assert resolve_to_stored_path("foo.py", "") is None


def test_missing_db_returns_none():
    assert resolve_to_stored_path("foo.py", "/nonexistent/path.db") is None


def test_is_known_positive(graph_db):
    assert is_known("src/groundtruth/foo.py", graph_db) is True


def test_is_known_negative(graph_db):
    assert is_known("not/a/file.py", graph_db) is False


def test_is_known_ambiguous(graph_db):
    assert is_known("dup.py", graph_db) is False
