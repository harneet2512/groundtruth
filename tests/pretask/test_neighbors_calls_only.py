"""Regression (I2/I6): the call-neighbor surface is CALLS-only — a promoted edge
(CO_SERIALIZES / standalone DATA_FLOW) must not add a neighbor to the "Calls:" surface.

Found by the per-layer LIPI (wx2eywcer): _issue_relevant_neighbors + the 1-hop expansion
JOINed edges untyped, so the depth landing silently broadened call-neighbors to promoted
edges. _static_callees (the no-issue-terms sibling) was already CALLS-typed.
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.v1r_brief import _issue_relevant_neighbors


def _graph(tmp_path):
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "file_path TEXT, is_test INTEGER, language TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, resolution_method TEXT, confidence REAL)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,?,?)",
        [
            (1, "Function", "fa", "a.py", 0, "python"),
            (2, "Function", "fb", "b.py", 0, "python"),  # CALLS neighbor (kept)
            (3, "Function", "fc", "c.py", 0, "python"),  # promoted neighbor (excluded)
        ],
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,2,'CALLS','import',1.0)"
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,3,'CO_SERIALIZES','promote_serde',1.0)"
    )
    conn.commit()
    conn.close()
    return db


def test_issue_relevant_neighbors_are_calls_only(tmp_path):
    db = _graph(tmp_path)
    out = _issue_relevant_neighbors(db, "a.py", str(tmp_path), {"anything"})
    assert "b.py" in out  # CALLS neighbor present
    assert "c.py" not in out  # promoted CO_SERIALIZES neighbor excluded
