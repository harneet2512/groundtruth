"""Tests for V1R brief generator."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _top_functions,
    _test_files_for,
    render_brief,
    generate_v1r_brief,
)


@pytest.fixture
def graph_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
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
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES nodes(id),
            target_id INTEGER NOT NULL REFERENCES nodes(id),
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'login_user', 'src/auth/handler.py', 0),
            (2, 'Function', 'verify_token', 'src/auth/handler.py', 0),
            (3, 'Function', 'test_login', 'tests/test_auth.py', 1),
            (4, 'Function', 'test_verify', 'tests/test_auth.py', 1),
            (5, 'Function', 'require_auth', 'src/auth/middleware.py', 0);
        INSERT INTO edges (source_id, target_id, type, confidence) VALUES
            (3, 1, 'CALLS', 1.0),
            (4, 2, 'CALLS', 1.0),
            (5, 2, 'CALLS', 1.0);
    """)
    conn.close()
    return db_path


def test_top_functions(graph_db: str) -> None:
    funcs = _top_functions(graph_db, "src/auth/handler.py")
    assert "verify_token" in funcs
    assert "login_user" in funcs
    assert len(funcs) <= 3


def test_top_functions_returns_by_ref_count(graph_db: str) -> None:
    funcs = _top_functions(graph_db, "src/auth/handler.py")
    assert funcs[0] == "verify_token"


def test_test_files_for(graph_db: str) -> None:
    tests = _test_files_for(graph_db, "src/auth/handler.py")
    assert "tests/test_auth.py" in tests


def test_test_files_empty_for_unknown(graph_db: str) -> None:
    tests = _test_files_for(graph_db, "nonexistent.py")
    assert tests == []


def test_render_brief_no_prose() -> None:
    files = [
        FileEntry(
            path="src/auth/handler.py",
            score=0.9,
            functions=["login_user", "verify_token"],
            test_mappings=["tests/test_auth.py"],
        ),
        FileEntry(
            path="src/auth/middleware.py",
            score=0.7,
            functions=["require_auth"],
            test_mappings=[],
        ),
    ]
    text = render_brief(files)
    assert text.startswith("<gt-task-brief>")
    assert text.endswith("</gt-task-brief>")
    assert "login_user" in text
    assert "require_auth" in text
    assert "Tests: tests/test_auth.py" in text
    for forbidden in [
        "justification",
        "constraint",
        "CONSTRAINT",
        "mirror",
        "scaffold",
        "editing elsewhere",
        "Edit existing",
        "Do not",
        "IMPLEMENTATION",
        "PATTERN",
        "CONTRACT",
        "SIDE FILES",
    ]:
        assert forbidden not in text, f"Brief must not contain prose: '{forbidden}'"


def test_render_brief_numbered() -> None:
    files = [
        FileEntry(path="a.py", score=1.0, functions=["foo"], test_mappings=[]),
        FileEntry(path="b.py", score=0.5, functions=["bar"], test_mappings=[]),
    ]
    text = render_brief(files)
    assert "1. a.py" in text
    assert "2. b.py" in text


@patch("groundtruth.pretask.v1r_brief.run_v74")
def test_generate_v1r_brief_empty_on_no_signal(mock_v74: MagicMock) -> None:
    mock_v74.return_value = MagicMock(ranked_full=[])
    result = generate_v1r_brief("fix auth bug", "/repo", "/db.sqlite")
    assert result.files == []
    assert "<gt-task-brief>" in result.brief_text


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=[])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_generate_v1r_brief_emits_low_score_candidates(_t, _f, mock_v74: MagicMock) -> None:
    mock_v74.return_value = MagicMock(
        ranked_full=[{"path": "a.py", "score": 0.1}]
    )
    result = generate_v1r_brief("fix auth bug", "/repo", "/db.sqlite")
    assert len(result.files) == 1
    assert result.files[0].path == "a.py"
    assert "1. a.py" in result.brief_text


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=["foo"])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=["tests/test_a.py"])
def test_generate_v1r_brief_respects_max_files(_mock_tests, _mock_funcs, mock_v74) -> None:
    mock_v74.return_value = MagicMock(
        ranked_full=[{"path": f"file{i}.py", "score": 0.9 - i * 0.1} for i in range(10)]
    )
    result = generate_v1r_brief("fix bug", "/repo", "/db.sqlite", max_files=3)
    assert len(result.files) <= 3


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=["foo"])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_generate_v1r_brief_token_cap(_mock_tests, _mock_funcs, mock_v74) -> None:
    mock_v74.return_value = MagicMock(
        ranked_full=[{"path": f"very/long/path/to/file{i}.py", "score": 0.9} for i in range(10)]
    )
    result = generate_v1r_brief("fix bug", "/repo", "/db.sqlite", max_brief_tokens=100)
    assert result.token_estimate <= 100


@pytest.fixture
def sparse_graph_db(tmp_path: Path) -> str:
    """Graph DB with < 2 edges per file — triggers sparse mode."""
    db_path = str(tmp_path / "sparse.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
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
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES nodes(id),
            target_id INTEGER NOT NULL REFERENCES nodes(id),
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.5,
            metadata TEXT
        );
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'parse_url', 'src/urls.py', 0),
            (2, 'Function', 'validate', 'src/validator.py', 0),
            (3, 'Function', 'render_page', 'src/render.py', 0),
            (4, 'Function', 'test_parse', 'tests/test_urls.py', 1);
        INSERT INTO edges (source_id, target_id, type, confidence) VALUES
            (4, 1, 'CALLS', 1.0);
    """)
    conn.close()
    return db_path


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=[])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_sparse_graph_no_suppression(
    _mock_tests, _mock_funcs, mock_v74, sparse_graph_db: str
) -> None:
    """On sparse graphs, modulus gate must NOT suppress the brief."""
    mock_v74.return_value = MagicMock(
        ranked_full=[
            {"path": "src/urls.py", "score": 0.8, "components": {"path": 0.0}},
            {"path": "src/validator.py", "score": 0.7, "components": {"path": 0.0}},
            {"path": "src/render.py", "score": 0.6, "components": {"path": 0.0}},
        ]
    )
    result = generate_v1r_brief(
        "fix url parsing bug", "/repo", sparse_graph_db
    )
    assert result.brief_text != ""
    assert len(result.files) > 0


@patch("groundtruth.pretask.v1r_brief.run_v74")
@patch("groundtruth.pretask.v1r_brief._top_functions", return_value=[])
@patch("groundtruth.pretask.v1r_brief._test_files_for", return_value=[])
def test_path_match_preservation(
    _mock_tests, _mock_funcs, mock_v74, tmp_path: Path
) -> None:
    """Files with strong path-name match must survive into top-5 even if BM25-outranked."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            qualified_name TEXT, file_path TEXT, start_line INTEGER,
            end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT DEFAULT 'python', parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.5, metadata TEXT
        );
        INSERT INTO nodes (id, label, name, file_path, is_test) VALUES
            (1, 'Function', 'foo', 'src/hub1.py', 0),
            (2, 'Function', 'bar', 'src/hub2.py', 0),
            (3, 'Function', 'baz', 'src/hub3.py', 0),
            (4, 'Function', 'qux', 'src/hub4.py', 0),
            (5, 'Function', 'quux', 'src/hub5.py', 0),
            (6, 'Function', 'color_apply', 'src/colorama.py', 0);
        INSERT INTO edges (source_id, target_id, type, confidence) VALUES
            (1, 2, 'CALLS', 1.0),
            (2, 3, 'CALLS', 1.0),
            (3, 4, 'CALLS', 1.0);
    """)
    conn.close()

    mock_v74.return_value = MagicMock(
        ranked_full=[
            {"path": "src/hub1.py", "score": 0.9, "components": {"path": 0.0}},
            {"path": "src/hub2.py", "score": 0.85, "components": {"path": 0.0}},
            {"path": "src/hub3.py", "score": 0.80, "components": {"path": 0.0}},
            {"path": "src/hub4.py", "score": 0.75, "components": {"path": 0.0}},
            {"path": "src/hub5.py", "score": 0.70, "components": {"path": 0.0}},
            {"path": "src/colorama.py", "score": 0.30, "components": {"path": 0.7}},
        ]
    )
    result = generate_v1r_brief(
        "fix colorama color rendering issue", "/repo", db_path, max_files=5
    )
    paths = [f.path for f in result.files]
    assert "src/colorama.py" in paths, (
        f"Path-matched file should survive into brief, got: {paths}"
    )
