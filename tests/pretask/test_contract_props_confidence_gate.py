"""Regression (I1/I3): contract_map._read_props gates properties at the fact floor.

A property mined below confidence 0.5 is not a contract FACT and must not be delivered
(correct-or-quiet). B-25: a legacy schema without a `properties.confidence` column is
also correct-or-quiet — it CANNOT verify the mining confidence, so it returns {} rather
than falling back to an ungated query. 'boundary_condition' has no value-validator, so it
isolates the confidence gate cleanly.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.contract_map import _read_props


def _conn(with_confidence=True):
    conn = sqlite3.connect(":memory:")
    if with_confidence:
        conn.execute(
            "CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, "
            "value TEXT, confidence REAL, line INTEGER)"
        )
    else:
        conn.execute(
            "CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, "
            "value TEXT, line INTEGER)"
        )
    return conn


def test_low_confidence_property_is_dropped():
    conn = _conn(True)
    conn.executemany(
        "INSERT INTO properties (node_id,kind,value,confidence,line) VALUES (?,?,?,?,?)",
        [
            (1, "boundary_condition", "x > 0", 0.8, 1),   # >= floor -> kept
            (1, "boundary_condition", "y < 10", 0.3, 2),  # < floor  -> dropped
        ],
    )
    conn.commit()
    vals = _read_props(conn, [1]).get("boundary_condition", [])
    assert "x > 0" in vals
    assert "y < 10" not in vals


def test_legacy_schema_without_confidence_is_quiet():
    # B-25: no confidence column -> cannot verify -> correct-or-quiet (was permissive).
    conn = _conn(False)
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line) VALUES (1,'boundary_condition','x > 0',1)"
    )
    conn.commit()
    assert _read_props(conn, [1]) == {}


def test_null_confidence_property_is_dropped():
    """Graph-F5 (B-25 fail-CLOSED): a row whose ``confidence`` value is NULL (the
    column exists but the miner left it unset) is UNCONFIDENCE-ABLE and must be
    DROPPED, not rendered at max authority. The pre-fix ``COALESCE(confidence,1.0)``
    floated a NULL to 1.0 and passed it through the ``>= 0.5`` gate (fail-OPEN);
    ``COALESCE(confidence,0.0)`` (parity with the gateway's ``_fact_callers``) fails
    it closed."""
    conn = _conn(True)
    conn.executemany(
        "INSERT INTO properties (node_id,kind,value,confidence,line) VALUES (?,?,?,?,?)",
        [
            (1, "boundary_condition", "x > 0", 0.8, 1),   # >= floor -> kept
            (1, "boundary_condition", "z == 0", None, 2),  # NULL conf -> DROPPED (fail-closed)
        ],
    )
    conn.commit()
    vals = _read_props(conn, [1]).get("boundary_condition", [])
    assert "x > 0" in vals
    assert "z == 0" not in vals
