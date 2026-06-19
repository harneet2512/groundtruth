"""Invariant 2: L6 Actionability

Pre-submit review evidence must appear before AgentFinishAction.
Content appended after state=FINISHED is a dead write.

Violation = F2 in failure taxonomy.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


def create_graph_with_callers(db_path: str) -> None:
    """Create graph.db with exported function that has production callers."""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT, name TEXT, qualified_name TEXT, file_path TEXT,
        start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,
        is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
        language TEXT NOT NULL, parent_id INTEGER
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

    # Exported function
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, signature, "
        "is_exported, is_test, language) VALUES "
        "(1, 'Function', 'check', 'beancount/ops/balance.py', 48, "
        "'def check(entries, options_map):', 1, 0, 'python')"
    )
    # Production caller
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(2, 'Function', 'load_file', 'beancount/loader.py', 100, 1, 0, 'python')"
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence) VALUES (2, 1, 'CALLS', 1.0)"
    )
    # Test function with assertion
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(3, 'Function', 'test_check', 'tests/test_balance.py', 50, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (3, 1, 'assertEqual', 'assertEqual(len(errors), 0)', 55)"
    )

    conn.commit()
    conn.close()


def simulate_l6_review(db_path: str, edited_files: list[str],
                        is_finish_handler: bool = False) -> dict:
    """Simulate L6 review and return delivery status.

    Returns dict with:
    - content: the review text
    - delivery_status: DELIVERED_VISIBLE or DEAD_WRITE
    - has_test_suggestions: whether test suggestions were included
    """
    if not os.path.exists(db_path):
        return {"content": "", "delivery_status": "NOT_APPLICABLE", "has_test_suggestions": False}

    def _escape_like(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    review_parts = []
    test_suggestions = []

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
            review_parts.append(f"  PRESERVE: {r['name']} in {cf_n} -- {r['cc']} callers")

        # Test suggestions from assertions
        try:
            tests = conn.execute(
                "SELECT DISTINCT n.file_path, n.name FROM assertions a "
                "JOIN nodes n ON a.test_node_id = n.id "
                "JOIN nodes nt ON a.target_node_id = nt.id "
                "WHERE nt.file_path LIKE ? ESCAPE '\\' AND a.target_node_id > 0 LIMIT 3",
                (f"%{_escape_like(cf_n)}",),
            ).fetchall()
            for t in tests:
                test_suggestions.append(f"  pytest {t['file_path']}::{t['name']}")
        except Exception:
            pass

    conn.close()

    content = ""
    if review_parts or test_suggestions:
        content = "[REVIEW] Changed files have dependents:\n" + "\n".join(review_parts[:8])
        if test_suggestions:
            content += "\nSuggested verification:\n" + "\n".join(test_suggestions[:5])

    delivery_status = "DEAD_WRITE" if is_finish_handler else "DELIVERED_VISIBLE"

    return {
        "content": content,
        "delivery_status": delivery_status,
        "has_test_suggestions": len(test_suggestions) > 0,
    }


class TestL6ActionabilityInvariant:
    """L6 review must be DELIVERED_VISIBLE, not DEAD_WRITE."""

    def test_post_edit_delivery_is_visible(self):
        """L6 review at post-edit time (not finish handler) is DELIVERED_VISIBLE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_callers(db_path)

            result = simulate_l6_review(
                db_path, ["beancount/ops/balance.py"],
                is_finish_handler=False,
            )
            assert result["delivery_status"] == "DELIVERED_VISIBLE"
            assert "[REVIEW]" in result["content"]
            assert "PRESERVE:" in result["content"]

    def test_finish_handler_delivery_is_dead_write(self):
        """L6 review in finish handler is DEAD_WRITE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_callers(db_path)

            result = simulate_l6_review(
                db_path, ["beancount/ops/balance.py"],
                is_finish_handler=True,
            )
            assert result["delivery_status"] == "DEAD_WRITE", (
                "Invariant 2: finish handler delivery must be DEAD_WRITE"
            )

    def test_review_includes_test_suggestions(self):
        """L6 review must include test suggestions when assertions exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_callers(db_path)

            result = simulate_l6_review(
                db_path, ["beancount/ops/balance.py"],
                is_finish_handler=False,
            )
            assert result["has_test_suggestions"], (
                "Invariant 2: L6 review must include test suggestions from assertions table"
            )
            assert "pytest" in result["content"]
            assert "test_check" in result["content"]

    def test_no_callers_produces_empty(self):
        """Files without callers should not produce L6 review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_callers(db_path)

            result = simulate_l6_review(
                db_path, ["nonexistent/file.py"],
                is_finish_handler=False,
            )
            assert result["content"] == ""
            assert result["delivery_status"] == "DELIVERED_VISIBLE"  # status is about timing, not content
