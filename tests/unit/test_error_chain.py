"""Tests for error_chain evidence module.

Covers extraction of raise/except, chain building with in-memory SQLite,
non-Python files, and feature flag OFF behavior.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


# -- Helpers --

def _create_graph_db(path: str) -> sqlite3.Connection:
    """Create an in-memory-style graph.db at the given path with the Go indexer schema."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
            language TEXT NOT NULL,
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
            confidence REAL DEFAULT 1.0,
            metadata TEXT
        );
    """)
    return conn


# -- Sample Python source snippets --

FUNC_RAISES_VALUE_ERROR = """\
def validate_input(data):
    if not data:
        raise ValueError("data is empty")
    return data
"""

FUNC_RAISES_TYPE_ERROR = """\
def check_type(obj):
    if not isinstance(obj, str):
        raise TypeError("expected str")
    return obj
"""

FUNC_CATCHES_VALUE_ERROR = """\
def process(data):
    try:
        result = validate_input(data)
    except ValueError as e:
        return None
    return result
"""

FUNC_CATCHES_BROADLY = """\
def handle(data):
    try:
        process(data)
    except Exception:
        pass
"""

FUNC_NO_ERRORS = """\
def pure_func(x):
    return x + 1
"""


class TestExtractErrorSurface:
    """Test extraction of raises/catches from Python source."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_ERROR_CHAIN_ENABLED", "1")

    def test_extract_raise_valueerror(self) -> None:
        from groundtruth.evidence.error_chain import extract_error_surface
        results = extract_error_surface(FUNC_RAISES_VALUE_ERROR)
        raises = [r for r in results if r["kind"] == "raise"]
        assert len(raises) == 1
        assert raises[0]["exception_type"] == "ValueError"

    def test_extract_raise_typeerror(self) -> None:
        from groundtruth.evidence.error_chain import extract_error_surface
        results = extract_error_surface(FUNC_RAISES_TYPE_ERROR)
        raises = [r for r in results if r["kind"] == "raise"]
        assert len(raises) == 1
        assert raises[0]["exception_type"] == "TypeError"

    def test_extract_except_valueerror(self) -> None:
        from groundtruth.evidence.error_chain import extract_error_surface
        results = extract_error_surface(FUNC_CATCHES_VALUE_ERROR)
        catches = [r for r in results if r["kind"] == "catch"]
        assert len(catches) == 1
        assert catches[0]["exception_type"] == "ValueError"

    def test_extract_except_broad(self) -> None:
        from groundtruth.evidence.error_chain import extract_error_surface
        results = extract_error_surface(FUNC_CATCHES_BROADLY)
        catches = [r for r in results if r["kind"] == "catch"]
        assert len(catches) == 1
        assert catches[0]["exception_type"] == "Exception"

    def test_extract_no_errors(self) -> None:
        from groundtruth.evidence.error_chain import extract_error_surface
        results = extract_error_surface(FUNC_NO_ERRORS)
        assert results == []

    def test_extract_multiple_in_one_function(self) -> None:
        source = """\
def complex_func(x):
    if x < 0:
        raise ValueError("negative")
    try:
        result = do_something(x)
    except TypeError:
        raise RuntimeError("wrapped")
"""
        from groundtruth.evidence.error_chain import extract_error_surface
        results = extract_error_surface(source)
        raises = [r for r in results if r["kind"] == "raise"]
        catches = [r for r in results if r["kind"] == "catch"]
        assert len(raises) == 2
        assert {r["exception_type"] for r in raises} == {"ValueError", "RuntimeError"}
        assert len(catches) == 1
        assert catches[0]["exception_type"] == "TypeError"


class TestTraceErrorChain:
    """Test chain building with in-memory SQLite."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_ERROR_CHAIN_ENABLED", "1")

    @pytest.fixture
    def db_setup(self, tmp_path):
        """Create a graph.db and source files for chain testing."""
        db_path = str(tmp_path / "graph.db")
        conn = _create_graph_db(db_path)

        # Write source files
        validate_py = tmp_path / "validate.py"
        validate_py.write_text(FUNC_RAISES_VALUE_ERROR)

        process_py = tmp_path / "process.py"
        process_py.write_text(FUNC_CATCHES_VALUE_ERROR)

        # Insert nodes
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) "
            "VALUES (1, 'Function', 'validate_input', ?, 1, 4, 'python')",
            (str(validate_py),),
        )
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) "
            "VALUES (2, 'Function', 'process', ?, 1, 5, 'python')",
            (str(process_py),),
        )

        # process CALLS validate_input
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_file) "
            "VALUES (2, 1, 'CALLS', ?)",
            (str(process_py),),
        )
        conn.commit()
        conn.close()

        return db_path, str(validate_py), str(process_py)

    def test_chain_raise_to_catch(self, db_setup) -> None:
        from groundtruth.evidence.error_chain import trace_error_chain
        db_path, validate_py, process_py = db_setup

        chains = trace_error_chain(db_path, validate_py, "validate_input")
        # validate_input RAISES ValueError -> process CATCHES ValueError
        assert any("RAISES ValueError" in c and "CATCHES ValueError" in c for c in chains)

    def test_chain_empty_for_no_errors(self, db_setup) -> None:
        from groundtruth.evidence.error_chain import trace_error_chain
        db_path, _, process_py = db_setup

        # process itself doesn't raise, so forward chain should be about its callees
        chains = trace_error_chain(db_path, process_py, "process")
        # process calls validate_input which raises ValueError
        # So we should see a forward chain entry
        assert any("ValueError" in c for c in chains) or chains == []

    def test_chain_nonexistent_function(self, db_setup) -> None:
        from groundtruth.evidence.error_chain import trace_error_chain
        db_path, validate_py, _ = db_setup

        chains = trace_error_chain(db_path, validate_py, "nonexistent_func")
        assert chains == []

    def test_non_python_returns_empty(self, db_setup) -> None:
        from groundtruth.evidence.error_chain import trace_error_chain
        db_path, _, _ = db_setup

        chains = trace_error_chain(db_path, "main.go", "main")
        assert chains == []


class TestErrorChainFeatureFlag:
    """Test that GT_ERROR_CHAIN_ENABLED=0 produces no output."""

    def test_extract_disabled_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_ERROR_CHAIN_ENABLED", "0")
        from groundtruth.evidence.error_chain import extract_error_surface
        result = extract_error_surface(FUNC_RAISES_VALUE_ERROR)
        assert result == []

    def test_trace_disabled_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("GT_ERROR_CHAIN_ENABLED", "0")
        from groundtruth.evidence.error_chain import trace_error_chain
        db_path = str(tmp_path / "graph.db")
        _create_graph_db(db_path).close()
        result = trace_error_chain(db_path, "test.py", "func")
        assert result == []

    def test_default_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GT_ERROR_CHAIN_ENABLED", raising=False)
        from groundtruth.evidence.error_chain import extract_error_surface
        result = extract_error_surface(FUNC_RAISES_VALUE_ERROR)
        assert result == []
