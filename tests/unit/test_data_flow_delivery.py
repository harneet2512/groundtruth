"""Delivery proof for the data_flow base in the contract pillar (contract_map).

The parser extracts a per-parameter forward slice (kind='data_flow'); the store
persists it (no whitelist). This test proves the LAST mile: the contract consumer
SELECTs kind='data_flow', the value validator gates it, and _fmt_one renders a
``flows:`` line the agent sees. In-memory sqlite — no temp file, so no Windows
graph.db file-lock teardown flake.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.contract_map import (
    ContractEvidence,
    _CONTRACT_KINDS,
    _fmt_one,
    _read_props,
    _valid_data_flow,
)


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE properties (node_id INTEGER, kind TEXT, value TEXT, "
        "line INTEGER, confidence REAL)"
    )
    return conn


def test_data_flow_is_a_contract_kind() -> None:
    # The SELECT in _read_props filters kind IN _CONTRACT_KINDS; data_flow must be in it
    # or the consumer never fetches the row.
    assert "data_flow" in _CONTRACT_KINDS


def test_read_props_surfaces_data_flow() -> None:
    conn = _mem_db()
    conn.execute(
        "INSERT INTO properties VALUES (?,?,?,?,?)",
        (1, "data_flow", "count -> validate(count) | count != 1 | count + 1", 10, 0.8),
    )
    props = _read_props(conn, [1])
    assert props.get("data_flow") == ["count -> validate(count) | count != 1 | count + 1"]


def test_fmt_one_renders_flows_line() -> None:
    # Full delivery: a ContractEvidence carrying flows renders a `flows:` line.
    ev = ContractEvidence(
        file="m.py",
        function="handle",
        flows=("count -> validate(count) | count != 1",),
    )
    assert ev.has_signal  # flows alone is signal (else render_contract drops it)
    out = _fmt_one(ev)
    assert "flows: count -> validate(count) | count != 1" in out


def test_malformed_data_flow_dropped() -> None:
    # Correct-or-quiet: a value whose arrow did not survive (just the bare param, or a
    # dangling fragment) carries no provenance and must be dropped by the validator.
    assert _valid_data_flow("count -> validate(count)")
    assert not _valid_data_flow("count")  # no arrow
    assert not _valid_data_flow("count -> ")  # empty RHS
    assert not _valid_data_flow("")


def test_read_props_drops_arrowless_data_flow() -> None:
    # End-to-end gate: an arrowless data_flow row is filtered by the value validator
    # in _read_props (not just the standalone validator).
    conn = _mem_db()
    conn.execute(
        "INSERT INTO properties VALUES (?,?,?,?,?)", (1, "data_flow", "count", 10, 0.8)
    )
    props = _read_props(conn, [1])
    assert "data_flow" not in props
