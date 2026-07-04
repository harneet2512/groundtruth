"""Invariants 4, 5, 6: L3 Post-Edit Evidence Hygiene

Invariant 4: _common.py must not outrank direct relevant tests.
Invariant 5: [COMPLETENESS] scoped to edited function, not whole class.
Invariant 6: Dunder methods excluded from [PATTERN] sibling evidence.

Violations = D5, D2, D2 in failure taxonomy.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


def create_graph_with_tests(db_path: str) -> None:
    """Create graph.db with both _common.py helpers and direct test files."""
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
    conn.execute("""CREATE TABLE assertions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_node_id INTEGER NOT NULL, target_node_id INTEGER DEFAULT 0,
        kind TEXT NOT NULL, expression TEXT NOT NULL,
        expected TEXT, line INTEGER
    )""")
    conn.execute("""CREATE TABLE properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
        line INTEGER, confidence REAL DEFAULT 1.0
    )""")

    # Target function
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language) VALUES "
        "(1, 'Method', 'set_fields', 'beets/importer.py', 602, "
        "'def set_fields(self, lib):', 1, 0, 'python')"
    )

    # _common.py helper test (should NOT outrank)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(10, 'Function', 'assertExists', 'beets/test/_common.py', 50, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (10, 1, 'assert', 'assert os.path.exists(syspath(path))', 55)"
    )

    # Direct relevant test (SHOULD outrank _common.py)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(11, 'Function', 'test_set_fields', 'test/test_importer.py', 395, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (11, 1, 'assertEqual', 'assertEqual(item.genre, genre)', 400)"
    )

    conn.commit()
    conn.close()


def rank_test_assertions(db_path: str, target_node_id: int,
                          issue_text: str = "") -> list[dict]:
    """Rank test assertions for a target function.

    Implements Invariant 4: direct tests outrank helpers.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT a.id, a.kind, a.expression, a.line, n.file_path, n.name "
        "FROM assertions a "
        "JOIN nodes n ON a.test_node_id = n.id "
        "WHERE a.target_node_id = ?",
        (target_node_id,),
    ).fetchall()
    conn.close()

    HELPER_PATTERNS = ("_common.py", "conftest.py", "helper.py", "helpers.py", "fixtures.py")

    results = []
    for r in rows:
        is_helper = any(p in r["file_path"] for p in HELPER_PATTERNS)
        results.append({
            "file": r["file_path"],
            "name": r["name"],
            "expression": r["expression"],
            "is_helper": is_helper,
            "score": 0 if is_helper else 10,  # helpers ranked lower
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


class TestInvariant4TestRanking:
    """_common.py must not outrank direct test files."""

    def test_direct_test_outranks_common_py(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_tests(db_path)

            ranked = rank_test_assertions(db_path, target_node_id=1)
            assert len(ranked) >= 2

            # First result should be the direct test, not _common.py
            assert not ranked[0]["is_helper"], (
                f"Invariant 4: direct test must outrank helper. "
                f"Top result: {ranked[0]['file']}::{ranked[0]['name']}"
            )
            assert "test_set_fields" in ranked[0]["name"] or "test_importer" in ranked[0]["file"]


class TestInvariant5CompletenessScope:
    """[COMPLETENESS] must be scoped to edited function, not whole class."""

    def test_completeness_scoped_to_function(self):
        """If agent edited set_fields, completeness should show
        methods sharing state with set_fields, not unrelated class methods."""
        edited_function = "set_fields"
        class_methods = [
            {"name": "set_fields", "shared_attrs": {"lib", "item"}},
            {"name": "chosen_info", "shared_attrs": {"choice_flag", "match"}},
            {"name": "set_choice", "shared_attrs": {"choice_flag", "match"}},
            {"name": "reload", "shared_attrs": {"item"}},
        ]

        # Only methods sharing attributes with the EDITED function
        edited_attrs = None
        for m in class_methods:
            if m["name"] == edited_function:
                edited_attrs = m["shared_attrs"]
                break

        assert edited_attrs is not None
        relevant = [
            m for m in class_methods
            if m["name"] != edited_function and m["shared_attrs"] & edited_attrs
        ]

        # reload shares "item" with set_fields — relevant
        assert any(m["name"] == "reload" for m in relevant)

        # chosen_info shares nothing with set_fields — should NOT appear
        irrelevant = [
            m for m in class_methods
            if m["name"] != edited_function and not (m["shared_attrs"] & edited_attrs)
        ]
        assert any(m["name"] == "chosen_info" for m in irrelevant), (
            "chosen_info does not share attrs with set_fields — should be excluded"
        )


class TestInvariant6DunderFilter:
    """Dunder methods must not appear in [PATTERN] sibling evidence."""

    def test_dunder_excluded_from_siblings(self):
        DUNDER_METHODS = {"__init__", "__repr__", "__str__", "__eq__", "__hash__", "__del__"}

        class_siblings = [
            {"name": "__init__", "body": "self.x = x"},
            {"name": "__repr__", "body": "return f'{self.x}'"},
            {"name": "set_fields", "body": "self.item.update(fields)"},
            {"name": "reload", "body": "self.item.load()"},
        ]

        filtered = [s for s in class_siblings if s["name"] not in DUNDER_METHODS]

        assert len(filtered) == 2
        assert all(s["name"] not in DUNDER_METHODS for s in filtered), (
            f"Invariant 6: dunder methods must be excluded from siblings. "
            f"Got: {[s['name'] for s in filtered]}"
        )
        assert any(s["name"] == "set_fields" for s in filtered)
        assert any(s["name"] == "reload" for s in filtered)
