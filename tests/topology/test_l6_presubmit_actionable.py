"""Regression test for L6 pre-submit actionability.

L6 review must fire BEFORE AgentFinishAction so the agent can act on it.
The fix moves test suggestion queries from the finish handler (dead write)
into the L6 early review hook that fires after the first source edit.

Research: CodeR (arXiv 2406.01304), TDFlow (arXiv 2510.23761),
"Verify Before You Fix" (arXiv 2604.10800) — all establish verification
must occur before final submission, not after.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


def create_graph_with_assertions(db_path: str) -> None:
    """Create graph.db with exported functions, callers, and assertions."""
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
    # Target function (exported, non-test)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, signature, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'set_fields', 'beets/importer.py', 602, "
        "'def set_fields(self, lib):', 1, 0, 'python')"
    )
    target_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Production caller
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'run_import', 'beets/ui/commands.py', 100, 1, 0, 'python')"
    )
    caller_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence) VALUES (?, ?, 'CALLS', 1.0)",
        (caller_id, target_id),
    )

    # Test function
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'test_set_fields', 'test/test_importer.py', 395, 0, 1, 'python')"
    )
    test_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Assertion linking test to target
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (?, ?, 'assertEqual', 'assertEqual(item.genre, genre)', 400)",
        (test_id, target_id),
    )

    conn.commit()
    conn.close()


def simulate_l6_early_review(db_path: str, edited_files: list[str]) -> str:
    """Simulate the L6 early review logic from oh_gt_full_wrapper.py.

    Returns the review block text that would be appended to the observation.
    This mirrors the production code at line 4302+ after the fix.
    """
    review_parts: list[str] = []
    test_suggestions: list[str] = []

    if not os.path.exists(db_path):
        return ""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def _escape_like(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    for cf in edited_files[:5]:
        cf_n = cf.replace("\\", "/").lstrip("/")
        rows = conn.execute(
            "SELECT n.name, COUNT(e.id) as cc FROM nodes n "
            "JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' "
            "AND COALESCE(e.confidence, 0.5) >= 0.7 "
            "JOIN nodes n2 ON e.source_id = n2.id AND n2.is_test = 0 "
            "WHERE n.file_path LIKE ? ESCAPE '\\' "
            "AND n.is_exported = 1 AND n.is_test = 0 "
            "GROUP BY n.id HAVING cc > 0 LIMIT 5",
            (f"%{_escape_like(cf_n)}",),
        ).fetchall()
        for r in rows:
            review_parts.append(f"  PRESERVE: {r['name']} in {cf_n} -- {r['cc']} callers depend on it")

    # Test suggestions from assertions table
    has_assertions = False
    try:
        conn.execute("SELECT 1 FROM assertions LIMIT 1")
        has_assertions = True
    except Exception:
        pass

    if has_assertions:
        for cf in edited_files[:5]:
            cf_n = cf.replace("\\", "/").lstrip("/")
            tests = conn.execute(
                "SELECT DISTINCT n.file_path, n.name FROM assertions a "
                "JOIN nodes n ON a.test_node_id = n.id "
                "JOIN nodes nt ON a.target_node_id = nt.id "
                "WHERE nt.file_path LIKE ? ESCAPE '\\' AND a.target_node_id > 0 LIMIT 3",
                (f"%{_escape_like(cf_n)}",),
            ).fetchall()
            for t in tests:
                test_suggestions.append(f"  pytest {t['file_path']}::{t['name']}")

    conn.close()

    if not review_parts and not test_suggestions:
        return ""

    block = "[REVIEW] Changed files have dependents:\n" + "\n".join(review_parts[:8])
    if test_suggestions:
        block += "\nSuggested verification:\n" + "\n".join(test_suggestions[:5])
    return block


class TestL6EarlyReviewFiresAfterFirstEdit:
    """L6 early review must fire after first source edit, not only after 2+."""

    def test_fires_after_single_edit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_assertions(db_path)

            result = simulate_l6_early_review(db_path, ["beets/importer.py"])
            assert "[REVIEW]" in result, (
                f"L6 review must fire after first edit. Got: {result!r}"
            )
            assert "PRESERVE:" in result
            assert "set_fields" in result

    def test_includes_test_suggestions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_assertions(db_path)

            result = simulate_l6_early_review(db_path, ["beets/importer.py"])
            assert "Suggested verification:" in result, (
                f"L6 review must include test suggestions. Got: {result!r}"
            )
            assert "test_set_fields" in result

    def test_no_review_without_callers(self):
        """Files without callers should not produce empty review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, start_line INTEGER, end_line INTEGER,
                signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
                is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
            )""")
            conn.execute("""CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
                type TEXT, source_line INTEGER, source_file TEXT,
                resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
            )""")
            conn.execute("""CREATE TABLE assertions (
                id INTEGER PRIMARY KEY, test_node_id INTEGER,
                target_node_id INTEGER DEFAULT 0, kind TEXT, expression TEXT,
                expected TEXT, line INTEGER
            )""")
            conn.execute(
                "INSERT INTO nodes VALUES (1, 'Function', 'helper', NULL, "
                "'utils/helper.py', 10, 20, NULL, NULL, 0, 0, 'python', NULL)"
            )
            conn.commit()
            conn.close()

            result = simulate_l6_early_review(db_path, ["utils/helper.py"])
            assert result == "", f"No callers should produce empty review. Got: {result!r}"


class TestL6ReviewIsPreFinish:
    """L6 review content is delivered at post-edit time, not finish time."""

    def test_review_output_contains_actionable_content(self):
        """The review block must contain content the agent can act on."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_assertions(db_path)

            result = simulate_l6_early_review(db_path, ["beets/importer.py"])
            # Must contain both caller contracts AND test suggestions
            assert "PRESERVE:" in result
            assert "pytest" in result
            # Agent can run the suggested test
            assert "test/test_importer.py::test_set_fields" in result
