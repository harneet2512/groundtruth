"""Regression (I1/I3): contract_map._read_props gates properties at the fact floor.

A property mined below confidence 0.5 is not a contract FACT and must not be delivered
(correct-or-quiet). A legacy schema without a `properties.confidence` column cannot
certify the property (B-25). 'boundary_condition' has no value-validator, so it
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
            (1, "boundary_condition", "x > 0", 0.8, 1),  # >= floor -> kept
            (1, "boundary_condition", "y < 10", 0.3, 2),  # < floor  -> dropped
        ],
    )
    conn.commit()
    vals = _read_props(conn, [1]).get("boundary_condition", [])
    assert "x > 0" in vals
    assert "y < 10" not in vals


def test_legacy_schema_without_confidence_cannot_certify_properties():
    conn = _conn(False)
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line) VALUES (1,'boundary_condition','x > 0',1)"
    )
    conn.commit()
    assert _read_props(conn, [1]).get("boundary_condition", []) == []
    assert conn.execute("SELECT value FROM properties").fetchone()[0] == "x > 0"
