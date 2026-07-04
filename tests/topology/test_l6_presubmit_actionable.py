"""Regression test for L6 pre-submit actionability (correct-or-quiet).

Originally this test mirrored an L6 "early review" block that emitted
"PRESERVE: {name} ... {cc} callers depend on it" — a caller-EDIT prescription.
That output was REMOVED per correct-or-quiet (SWE-PRM NeurIPS 2025:
action-prescriptive feedback lowers resolution; diagnostic helps).

The verifiable part L6 used to deliver — tests that cover the edited files,
from the assertions table (target_node_id > 0) — is now owned solely by
``_maybe_fire_presubmit_verify`` (see tests/unit/test_presubmit_verify.py).

This file now asserts the NEW contract:
  1. No "PRESERVE: ... callers depend on it" caller-prescription remains in
     the wrapper's L6-early paths.
  2. The single verifiable path still delivers pytest suggestions covering
     edited files (via the real ``_maybe_fire_presubmit_verify``).

Research: CodeR (arXiv 2406.01304), TDFlow (arXiv 2510.23761),
"Verify Before You Fix" (arXiv 2604.10800) — verification before submission;
SWE-PRM (NeurIPS 2025) — verifiable-not-prescriptive feedback.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))
import oh_gt_full_wrapper as w  # noqa: E402

WRAPPER_SRC = Path(w.__file__).read_text(encoding="utf-8")


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


class _Obs:
    def __init__(self, content: str = "agent output"):
        self.content = content


class TestNoCallerPrescriptionRemains:
    """correct-or-quiet: the L6-early caller-EDIT prescription is gone."""

    def test_no_preserve_callers_depend_string_in_wrapper(self):
        # The caller-prescription was emitted as an f-string of the form
        #   f"  PRESERVE: {name} in {file} -- {cc} callers depend on it"
        # Assert that emission pattern (PRESERVE: with an interpolated name) is
        # gone. We match the f-string emission marker, not the descriptive
        # phrase alone, so a removal-note comment cannot trip the check.
        assert "PRESERVE: {" not in WRAPPER_SRC, (
            "correct-or-quiet violation: caller-EDIT prescription "
            "'PRESERVE: {name} ... callers depend on it' still emitted in wrapper."
        )

    def test_no_l6_early_review_blocks(self):
        # Both the primary and legacy L6-early review blocks were removed; the
        # _l6_early_fired flag they used must no longer exist.
        assert "_l6_early_fired" not in WRAPPER_SRC, (
            "L6-early review state flag still present — block not fully removed."
        )


class TestVerifiableTestCoverageStillDelivered:
    """The verifiable path is owned by _maybe_fire_presubmit_verify."""

    def test_presubmit_verify_delivers_pytest_for_edited_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_assertions(db_path)

            cfg = w.GTRuntimeConfig()
            cfg.graph_db = db_path
            cfg._host_graph_db = db_path
            cfg._presubmit_edited_files = {"beets/importer.py"}
            cfg._presubmit_last_edit_action = 5
            cfg.action_count = 8  # >= 3 actions since last edit → review transition
            cfg._presubmit_fired = False

            obs = w._maybe_fire_presubmit_verify(cfg, _Obs(), None)
            content = getattr(obs, "content", "")
            assert cfg._presubmit_fired is True
            # Verifiable action only — the test covering the edited file.
            assert "pytest" in content
            assert "test/test_importer.py::test_set_fields" in content
            # And NOT a caller-edit prescription.
            assert "callers depend on it" not in content
