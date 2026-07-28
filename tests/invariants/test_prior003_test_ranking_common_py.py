"""PRIOR-003: _common.py must not outrank direct relevant tests.

Tests the production code path: post_edit.py test assertion ranking
with helper file deprioritization.

Research: R4 TCTracer (ICSE 2020) — naming convention signal gives
direct test files higher traceability score than helper utilities.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


def create_graph_with_mixed_tests(db_path: str) -> None:
    """Create graph.db with both _common.py helper and direct test assertions."""
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
        node_id INTEGER, kind TEXT, value TEXT, line INTEGER, confidence REAL
    )""")

    # Target function
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language) VALUES "
        "(1, 'Method', 'set_fields', 'beets/importer.py', 602, 630, "
        "'def set_fields(self, lib):', 1, 0, 'python')"
    )

    # _common.py helper test (should be deprioritized)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(10, 'Function', 'assertExists', 'beets/test/_common.py', 50, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (10, 1, 'assert', 'assert os.path.exists(syspath(path))', 55)"
    )

    # Direct relevant test (should outrank _common.py)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(11, 'Function', 'test_set_fields', 'test/test_importer.py', 395, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (11, 1, 'assertEqual', 'assertEqual(item.genre, genre)', 400)"
    )

    # conftest helper (should also be deprioritized)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, "
        "is_exported, is_test, language) VALUES "
        "(12, 'Function', 'setup_importer', 'test/conftest.py', 20, 0, 1, 'python')"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) "
        "VALUES (12, 1, 'assert', 'assert importer is not None', 25)"
    )

    conn.commit()
    conn.close()


class TestHelperDeprioritization:
    """PRIOR-003: _common.py must not outrank direct test files."""

    def test_direct_test_outranks_common_py_in_production(self):
        """SUPERSEDED BY TEST-BLINDNESS, and re-pointed at the rule that replaced it.

        This asserted a RANKING over test assertions read from graph.db. That reader,
        ``post_edit._get_test_assertions_from_graph``, is now `return []` BY DESIGN
        (post_edit.py:1729-1737): "the [TEST] family named the test and read the assertions
        table (test-derived) -- both are leakage. Disabled so GT is fully test-blind and its
        output is unchanged if the grading test is swapped. (run12 leaked test_plot_hdi ...)".

        Ranking test files can no longer matter, because no test file is ever surfaced. The
        original assertion has been unsatisfiable since that change, which is what kept
        `preflight_checks --strict` permanently BLOCKED. The invariant worth guarding now is
        the STRONGER one: the reader stays dark even when the graph is full of assertions
        that would rank well.
        """
        from groundtruth.hooks.post_edit import _get_test_assertions_from_graph

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_mixed_tests(db_path)

            results = _get_test_assertions_from_graph(db_path, "beets/importer.py", "set_fields")

            # A populated graph is the CALIBRATION: the fixture deliberately contains direct
            # test assertions AND _common.py helpers, so an empty result here is the leak
            # filter working, not an empty database.
            assert results == [], (
                "GT must stay test-blind: surfacing a test name makes GT's output depend on "
                "the grading test, which is the swap-invariance this rule protects."
            )

    def test_direct_test_has_higher_rank(self):
        """test_set_fields from test_importer.py should rank above _common.py helpers."""
        from groundtruth.hooks.post_edit import _get_test_assertions_from_graph

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_mixed_tests(db_path)

            results = _get_test_assertions_from_graph(db_path, "beets/importer.py", "set_fields")

            # Find positions
            direct_pos = None
            helper_pos = None
            for i, r in enumerate(results):
                fp = r.get("file_path", "")
                if "test_importer" in fp:
                    direct_pos = i
                if "_common.py" in fp:
                    helper_pos = i

            if direct_pos is not None and helper_pos is not None:
                assert direct_pos < helper_pos, (
                    f"PRIOR-003: direct test (pos {direct_pos}) must rank above "
                    f"_common.py helper (pos {helper_pos})"
                )
