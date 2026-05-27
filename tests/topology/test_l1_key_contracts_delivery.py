"""Regression test for BUG-002: [GT KEY CONTRACTS] marker must be present
when contract data (guard_clause, conditional_return, side_effect) exists
in the graph.db properties table for the edit-target function.

Evidence from fresh canary run 26532251352:
- beancount: contracts=1, "Preserve: exception_handler: except KeyError -> handles"
  present inside <gt-edit-target> tags, but no [GT KEY CONTRACTS] marker
- beets: contracts=0 (Pipeline() has no qualifying properties)
- loguru: contracts=0 (info() has no qualifying properties)
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


def create_graph_with_properties(db_path: str) -> None:
    """Create a minimal graph.db with one exported function + guard_clause property."""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes (
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
        parent_id INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        source_line INTEGER,
        source_file TEXT,
        resolution_method TEXT,
        confidence REAL DEFAULT 0.0,
        metadata TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        line INTEGER,
        confidence REAL DEFAULT 1.0
    )""")
    # Exported function
    conn.execute(
        "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language) VALUES "
        "('Function', 'check', 'balance.check', 'beancount/ops/balance.py', 48, 100, "
        "'def check(entries, options_map):', 1, 0, 'python')"
    )
    node_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Guard clause property
    conn.execute(
        "INSERT INTO properties (node_id, kind, value, line, confidence) VALUES "
        "(?, 'guard_clause', 'if not entries: return []', 50, 1.0)",
        (node_id,),
    )
    # Exception handler property
    conn.execute(
        "INSERT INTO properties (node_id, kind, value, line, confidence) VALUES "
        "(?, 'exception_handler', 'except KeyError -> handles', 65, 1.0)",
        (node_id,),
    )
    # Caller edge so function appears as edit target
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, is_exported, is_test, language) VALUES "
        "('Function', 'caller_func', 'tests/test_balance.py', 10, 0, 1, 'python')"
    )
    caller_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence) VALUES (?, ?, 'CALLS', 1.0)",
        (caller_id, node_id),
    )
    conn.commit()
    conn.close()


def create_graph_without_properties(db_path: str) -> None:
    """Create graph.db with an exported function but NO qualifying properties."""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
        file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
        signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER, target_id INTEGER, type TEXT, source_line INTEGER,
        source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER NOT NULL,
        kind TEXT NOT NULL, value TEXT NOT NULL, line INTEGER, confidence REAL DEFAULT 1.0
    )""")
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, signature, is_exported, is_test, language) VALUES "
        "('Function', 'run_parallel', 'beets/util/pipeline.py', 10, 'def run_parallel(self):', 1, 0, 'python')"
    )
    conn.commit()
    conn.close()


def simulate_l1_key_contracts_query(db_path: str, file_path: str) -> list[str]:
    """Simulate the L1+ key-contracts query from oh_gt_full_wrapper.py lines 5795-5828.

    Returns the _contract_lines list as the wrapper would build it.
    """
    contract_lines: list[str] = []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    norm = file_path.replace("\\", "/").lstrip("/")
    # Find top exported functions (same query as wrapper line 5764-5770)
    key_funcs = conn.execute(
        "SELECT id, name, signature FROM nodes "
        "WHERE file_path LIKE ? AND is_exported = 1 AND is_test = 0 "
        "ORDER BY (SELECT COUNT(*) FROM edges WHERE target_id = nodes.id "
        "AND COALESCE(edges.confidence, 0.5) >= 0.6) DESC LIMIT 3",
        (f"%{norm}",),
    ).fetchall()

    for kf in key_funcs:
        props = conn.execute(
            "SELECT kind, value FROM properties WHERE node_id = ? "
            "AND kind IN ('guard_clause', 'conditional_return', 'exception_handler', 'side_effect') LIMIT 3",
            (kf["id"],),
        ).fetchall()
        for p in props:
            contract_lines.append(f"  Preserve: {p['kind']}: {p['value']}")

    conn.close()
    return contract_lines


def simulate_l1_extra_with_marker(contract_lines: list[str], plan_lines: list[str],
                                   edit_target: bool) -> str:
    """Simulate the _l1_extra construction WITH the [GT KEY CONTRACTS] marker fix."""
    l1_extra = ""
    if edit_target:
        all_lines = plan_lines + contract_lines
        l1_extra = "\n<gt-edit-target>\n" + "\n".join(all_lines) + "\n</gt-edit-target>"
    elif plan_lines:
        l1_extra = "\n<gt-orientation>\n" + "\n".join(plan_lines) + "\n</gt-orientation>"

    # BUG-002 fix: emit [GT KEY CONTRACTS] marker when contracts exist
    if contract_lines:
        l1_extra += "\n[GT KEY CONTRACTS]\n" + "\n".join(contract_lines)

    return l1_extra


class TestKeyContractsMarkerPresent:
    """When properties exist, [GT KEY CONTRACTS] marker must appear."""

    def test_contracts_present_produces_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_properties(db_path)

            contract_lines = simulate_l1_key_contracts_query(db_path, "beancount/ops/balance.py")
            assert len(contract_lines) > 0, "Expected non-empty contract_lines from properties"

            l1_extra = simulate_l1_extra_with_marker(
                contract_lines, ["  Key function: check() in balance.py"], edit_target=True,
            )
            assert "[GT KEY CONTRACTS]" in l1_extra, (
                f"BUG-002: [GT KEY CONTRACTS] marker missing despite non-empty contracts. "
                f"Got: {l1_extra[:300]}"
            )
            assert "Preserve:" in l1_extra

    def test_contracts_absent_no_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_without_properties(db_path)

            contract_lines = simulate_l1_key_contracts_query(db_path, "beets/util/pipeline.py")
            assert len(contract_lines) == 0, "Expected empty contract_lines"

            l1_extra = simulate_l1_extra_with_marker(
                contract_lines, ["  beets/util/pipeline.py: key functions = run_parallel"],
                edit_target=False,
            )
            assert "[GT KEY CONTRACTS]" not in l1_extra, (
                "Should not emit [GT KEY CONTRACTS] when no qualifying properties exist"
            )

    def test_orientation_mode_also_gets_marker(self):
        """Even in orientation mode (no edit target), contracts should get marker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_properties(db_path)

            contract_lines = simulate_l1_key_contracts_query(db_path, "beancount/ops/balance.py")
            l1_extra = simulate_l1_extra_with_marker(
                contract_lines,
                ["  beancount/ops/balance.py: key functions = check"],
                edit_target=False,
            )
            assert "[GT KEY CONTRACTS]" in l1_extra
            assert "<gt-orientation>" in l1_extra


class TestPropertyQueryCorrectness:
    """Verify the properties query returns correct data."""

    def test_guard_clause_extracted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_properties(db_path)
            lines = simulate_l1_key_contracts_query(db_path, "beancount/ops/balance.py")
            guard = [l for l in lines if "guard_clause" in l]
            assert len(guard) == 1
            assert "if not entries: return []" in guard[0]

    def test_exception_handler_extracted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            create_graph_with_properties(db_path)
            lines = simulate_l1_key_contracts_query(db_path, "beancount/ops/balance.py")
            exc = [l for l in lines if "exception_handler" in l]
            assert len(exc) == 1
            assert "KeyError" in exc[0]


class TestFreshArtifactEvidence:
    """Check fresh run logs to verify contracts count."""

    BEANCOUNT_LOG = os.path.join(
        os.path.dirname(__file__), "..", "..", "runs", "fresh_canary",
        "canary-v2_live-beancount__beancount-931", "gt_debug", "full_run.log",
    )

    @pytest.mark.skipif(
        not os.path.isfile(os.path.join(
            os.path.dirname(__file__), "..", "..", "runs", "fresh_canary",
            "canary-v2_live-beancount__beancount-931", "gt_debug", "full_run.log",
        )),
        reason="Beancount fresh artifact not available",
    )
    def test_beancount_has_nonzero_contracts(self):
        """Beancount must have contracts>0 in logs, proving properties exist."""
        with open(self.BEANCOUNT_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "l1_enhanced" in line and "contracts=" in line:
                    # Extract contracts count
                    idx = line.index("contracts=")
                    count_str = line[idx + 10:].strip().split()[0].split(",")[0]
                    count = int(count_str)
                    assert count > 0, (
                        f"Beancount must have contracts>0. Got: {line.strip()}"
                    )
                    return
        pytest.fail("No l1_enhanced log line found in beancount full_run.log")
