"""Regression test for PRIOR-005: vendor JS files (jquery.js, etc.) must be
excluded from caller evidence in L3b post-view and L5b scope warnings.

Reproduced from old run artifact: gt_layer_events beets showed
beetsplug/web/static/jquery.js:8547 in Called by: lines.

Tests cover:
1. post_view.py graph_navigation() — callers must exclude vendor JS
2. governor.py _check_multi_file_scope() — scope warnings must exclude vendor JS
3. Legitimate Python callers must NOT be filtered
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

# Import production code
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


def create_graph_with_vendor_callers(db_path: str) -> None:
    """Create graph.db where target function has callers including vendor JS."""
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
        confidence REAL DEFAULT 0.0, metadata TEXT,
        trust_tier TEXT DEFAULT 'SPECULATIVE',
        candidate_count INTEGER DEFAULT 1,
        evidence_type TEXT,
        verification_status TEXT DEFAULT 'unverified'
    )""")
    conn.execute("""CREATE TABLE properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
        line INTEGER, confidence REAL DEFAULT 1.0
    )""")

    # Target function (the one being viewed)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language) VALUES "
        "('Function', 'ImportTask', 'beets/importer.py', 100, 200, "
        "'class ImportTask:', 1, 0, 'python')"
    )
    target_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Legitimate Python caller
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'run_bench', 'beetsplug/bench.py', 79, 1, 0, 'python')"
    )
    bench_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (?, ?, 'CALLS', 1.0, 'import')",
        (bench_id, target_id),
    )

    # Vendor JS caller (should be filtered)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'each', 'beetsplug/web/static/jquery.js', 8547, 1, 0, 'javascript')"
    )
    jquery_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (?, ?, 'CALLS', 0.9, 'name_match')",
        (jquery_id, target_id),
    )

    # Another vendor JS (node_modules)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'init', 'node_modules/lodash/lodash.min.js', 1, 1, 0, 'javascript')"
    )
    lodash_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (?, ?, 'CALLS', 0.8, 'name_match')",
        (lodash_id, target_id),
    )

    # Test caller (should appear normally)
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "('Function', 'test_import', 'test/test_importer.py', 10, 0, 1, 'python')"
    )
    test_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (?, ?, 'CALLS', 1.0, 'import')",
        (test_id, target_id),
    )

    conn.commit()
    conn.close()


class TestPostViewExcludesVendorJS:
    """post_view.py graph_navigation() must filter vendor JS from callers."""

    def test_jquery_excluded_from_callers(self):
        from groundtruth.hooks.post_view import graph_navigation

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_vendor_callers(db_path)

            lines, count = graph_navigation(
                "beets/importer.py", db_path, limit=5,
            )
            output = "\n".join(lines)

            assert "jquery" not in output.lower(), (
                f"PRIOR-005: jquery.js must not appear in caller evidence. "
                f"Got:\n{output}"
            )
            assert "lodash" not in output.lower(), (
                f"node_modules vendor JS must not appear. Got:\n{output}"
            )

    def test_legitimate_callers_preserved(self):
        from groundtruth.hooks.post_view import graph_navigation

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_vendor_callers(db_path)

            lines, count = graph_navigation(
                "beets/importer.py", db_path, limit=5,
            )
            output = "\n".join(lines)

            assert "bench.py" in output, (
                f"Legitimate Python caller must appear. Got:\n{output}"
            )


class TestGovernorExcludesVendorJS:
    """governor.py _check_multi_file_scope() must filter vendor JS."""

    def test_jquery_excluded_from_scope_warnings(self):
        from groundtruth.trajectory.governor import L5Governor

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_vendor_callers(db_path)

            old_env = os.environ.get("GT_GRAPH_DB")
            os.environ["GT_GRAPH_DB"] = db_path
            try:
                gov = L5Governor(instance_id="test_vendor_js")
                gov.state.edited_source_files = ["beets/importer.py"]

                result = gov._check_multi_file_scope()

                assert "jquery" not in result.lower(), (
                    f"PRIOR-005: jquery.js must not appear in scope warnings. "
                    f"Got:\n{result}"
                )
                assert "lodash" not in result.lower(), (
                    f"node_modules vendor JS must not appear. Got:\n{result}"
                )
            finally:
                if old_env is not None:
                    os.environ["GT_GRAPH_DB"] = old_env
                else:
                    os.environ.pop("GT_GRAPH_DB", None)

    def test_legitimate_callers_in_scope_warning(self):
        from groundtruth.trajectory.governor import L5Governor

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_vendor_callers(db_path)

            old_env = os.environ.get("GT_GRAPH_DB")
            os.environ["GT_GRAPH_DB"] = db_path
            try:
                gov = L5Governor(instance_id="test_vendor_js_legit")
                gov.state.edited_source_files = ["beets/importer.py"]

                result = gov._check_multi_file_scope()

                if result:
                    assert "bench.py" in result, (
                        f"Legitimate caller must appear in scope warning. Got:\n{result}"
                    )
            finally:
                if old_env is not None:
                    os.environ["GT_GRAPH_DB"] = old_env
                else:
                    os.environ.pop("GT_GRAPH_DB", None)
