"""Contract tests: GraphStore must override ALL public SymbolStore methods.

These tests ensure that no SymbolStore method falls through to Python-schema
queries when operating on a Go-schema graph.db.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile

import pytest

from groundtruth.index.graph_store import GraphStore
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err


def _public_methods(cls: type) -> set[str]:
    """Get all public method names (non-underscore) of a class."""
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_all_methods_overridden() -> None:
    """Every public method of SymbolStore must be overridden in GraphStore."""
    parent_methods = _public_methods(SymbolStore)
    assert parent_methods, "SymbolStore has no public methods — test is broken"

    not_overridden = []
    for method in sorted(parent_methods):
        child_impl = getattr(GraphStore, method, None)
        parent_impl = getattr(SymbolStore, method)
        if child_impl is parent_impl or child_impl is None:
            not_overridden.append(method)

    assert not_overridden == [], (
        f"GraphStore methods fall through to SymbolStore (will crash on graph.db): "
        f"{', '.join(not_overridden)}"
    )


@pytest.fixture()
def graph_db_path() -> str:
    """Create a minimal Go-schema graph.db for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
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
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        CREATE TABLE file_hashes (
            file_path TEXT PRIMARY KEY,
            hash TEXT NOT NULL
        );
        CREATE TABLE project_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Seed data
        INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, language, is_exported)
        VALUES ('Function', 'main', 'pkg.main', 'src/main.py', 1, 10, 'python', 1);
        INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, language, is_exported)
        VALUES ('Function', 'helper', 'pkg.helper', 'src/main.py', 12, 20, 'python', 0);
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (1, 2, 'CALLS', 5, 'src/main.py', 'same_file', 1.0);
        INSERT INTO project_meta (key, value) VALUES ('build_time', '1.5s');
    """)
    conn.close()
    yield path
    os.unlink(path)


def test_no_crash_on_graph_db(graph_db_path: str) -> None:
    """Call every public GraphStore method with reasonable args — none may raise."""
    store = GraphStore(db_path=graph_db_path)
    init_result = store.initialize()
    assert not isinstance(init_result, Err), f"Init failed: {init_result.error.message}"

    # Read operations (should return Ok with data or empty results)
    read_calls = [
        ("find_symbol_by_name", ("main",)),
        ("get_symbols_in_file", ("src/main.py",)),
        ("get_symbol_by_id", (1,)),
        ("get_refs_from_file", ("src/main.py",)),
        ("get_refs_for_symbol", (1,)),
        ("get_imports_for_file", ("src/main.py",)),
        ("get_importers_of_file", ("src/main.py",)),
        ("get_all_symbol_names", ()),
        ("get_all_files", ()),
        ("get_exports_by_module", ("main",)),
        ("search_symbols_fts", ("main",)),
        ("get_stats", ()),
        ("get_dead_code", ()),
        ("get_unused_packages", ()),
        ("get_all_packages", ()),
        ("get_hotspots", ()),
        ("get_entry_point_files", ()),
        ("get_top_directories", ()),
        ("get_sibling_files", ("src/main.py",)),
        ("get_file_dependencies", ()),
        ("get_package", ("requests",)),
        ("get_latest_validation_id", ("test.py",)),
        ("get_briefing_logs_for_file", ("test.py",)),
        ("get_briefing_log", (1,)),
        ("get_recent_briefing_logs", ()),
        ("get_module_symbol_count", ("main",)),
        ("module_has_dynamic_exports", ("main",)),
        ("get_symbols_in_line_range", ("src/main.py", 1, 50)),
        ("get_file_metadata", ("test.py",)),
        ("get_all_file_metadata", ()),
        ("get_metadata", ("build_time",)),
        ("update_briefing_compliance", (1, 0.5, [], [], [])),
        ("link_briefing_to_validation", (1, 1)),
        ("rebuild_fts", ()),
    ]

    for name, args in read_calls:
        func = getattr(store, name)
        result = func(*args)
        assert not isinstance(result, Err), f"{name} returned Err: {result.error.message}"

    # Write operations (should return Err(read_only), not crash)
    write_calls = [
        ("insert_symbol", ()),
        ("delete_symbols_in_file", ("test.py",)),
        ("update_usage_count", (1, 5)),
        ("insert_ref", (1, "test.py", 1, "call")),
        ("insert_export", (1, "mod", False, True)),
        ("insert_package", ("pkg", "1.0", "pip", False)),
        ("upsert_file_metadata", ("test.py", 1.0, 100, 5, 1000)),
        ("delete_file_metadata", ("test.py",)),
        ("set_metadata", ("key", "val")),
        ("upsert_module_coverage", ("mod", 5, False, False, False, 1000)),
    ]

    for name, args in write_calls:
        func = getattr(store, name)
        result = func(*args)
        assert isinstance(result, Err), f"{name} should return Err(read_only), got Ok"

    # log_intervention is a stub that returns Ok(0), not Err
    log_result = store.log_intervention()
    assert not isinstance(log_result, Err)

    store.close()


def test_get_metadata_reads_project_meta(graph_db_path: str) -> None:
    """get_metadata should read from project_meta table in Go-schema DB."""
    store = GraphStore(db_path=graph_db_path)
    store.initialize()
    result = store.get_metadata("build_time")
    assert not isinstance(result, Err)
    assert result.value == "1.5s"
    store.close()


def test_get_symbols_in_line_range(graph_db_path: str) -> None:
    """get_symbols_in_line_range should query nodes table."""
    store = GraphStore(db_path=graph_db_path)
    store.initialize()
    result = store.get_symbols_in_line_range("src/main.py", 1, 25)
    assert not isinstance(result, Err)
    names = [s.name for s in result.value]
    assert "main" in names
    assert "helper" in names
    store.close()


def test_find_symbol_by_qualified_name(graph_db_path: str) -> None:
    """Qualified-name lookup should work on graph.db nodes."""
    store = GraphStore(db_path=graph_db_path)
    store.initialize()
    result = store.find_symbol_by_name("pkg.main")
    assert not isinstance(result, Err)
    assert [s.file_path for s in result.value] == ["src/main.py"]
    store.close()
