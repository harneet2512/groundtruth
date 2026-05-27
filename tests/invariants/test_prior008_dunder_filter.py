"""PRIOR-008 regression test: __init__ must not appear in sibling pattern evidence.

Tests the production code path: post_edit.py _get_siblings_from_graph().
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


def create_graph_with_dunder_siblings(db_path: str, repo_root: str) -> None:
    """Create graph.db where a class has __init__ + regular methods as siblings."""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
        file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
        signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
    )""")
    conn.execute("""CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER, target_id INTEGER, type TEXT,
        source_line INTEGER, source_file TEXT, resolution_method TEXT,
        confidence REAL DEFAULT 0.0, metadata TEXT
    )""")

    # Parent class
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_exported, is_test, language) VALUES "
        "(1, 'Class', 'ImportTask', 'beets/importer.py', 100, 700, 1, 0, 'python')"
    )
    # __init__ (should be filtered)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language, parent_id) VALUES "
        "(2, 'Method', '__init__', 'beets/importer.py', 110, 130, "
        "'def __init__(self, toppath, paths, items):', 0, 0, 'python', 1)"
    )
    # __repr__ (should be filtered)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language, parent_id) VALUES "
        "(3, 'Method', '__repr__', 'beets/importer.py', 135, 140, "
        "'def __repr__(self):', 0, 0, 'python', 1)"
    )
    # set_fields (target — we edit this)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language, parent_id) VALUES "
        "(4, 'Method', 'set_fields', 'beets/importer.py', 602, 630, "
        "'def set_fields(self, lib):', 1, 0, 'python', 1)"
    )
    # reload (valid sibling)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language, parent_id) VALUES "
        "(5, 'Method', 'reload', 'beets/importer.py', 640, 650, "
        "'def reload(self):', 1, 0, 'python', 1)"
    )

    # Create the source file so snippet reading works
    src_path = os.path.join(repo_root, "beets", "importer.py")
    os.makedirs(os.path.dirname(src_path), exist_ok=True)
    lines = [""] * 700
    lines[109] = "    def __init__(self, toppath, paths, items):\n"
    lines[110] = "        self.toppath = toppath\n"
    lines[134] = "    def __repr__(self):\n"
    lines[135] = "        return f'ImportTask({self.toppath})'\n"
    lines[601] = "    def set_fields(self, lib):\n"
    lines[602] = "        for field, value in self.fields.items():\n"
    lines[639] = "    def reload(self):\n"
    lines[640] = "        self.item.load()\n"
    with open(src_path, "w") as f:
        f.writelines(lines)

    conn.commit()
    conn.close()


class TestDunderFilterInProduction:
    """PRIOR-008: _get_siblings_from_graph must exclude dunder methods."""

    def test_init_excluded_from_siblings(self):
        from groundtruth.hooks.post_edit import _get_siblings_from_graph

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_dunder_siblings(db_path, tmpdir)

            siblings = _get_siblings_from_graph(
                db_path, "beets/importer.py", "set_fields", tmpdir,
            )

            names = [s["name"] for s in siblings]
            assert "__init__" not in names, (
                f"PRIOR-008: __init__ must be filtered from siblings. Got: {names}"
            )
            assert "__repr__" not in names, (
                f"PRIOR-008: __repr__ must be filtered from siblings. Got: {names}"
            )

    def test_valid_siblings_preserved(self):
        from groundtruth.hooks.post_edit import _get_siblings_from_graph

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_dunder_siblings(db_path, tmpdir)

            siblings = _get_siblings_from_graph(
                db_path, "beets/importer.py", "set_fields", tmpdir,
            )

            names = [s["name"] for s in siblings]
            assert "reload" in names, (
                f"Valid sibling 'reload' must be preserved. Got: {names}"
            )
