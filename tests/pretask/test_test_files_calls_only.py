"""Regression (G13, gt_new Appendix I:341): the related-test surface is CALLS-only.

`_test_files_for` (v1r_brief) feeds FileEntry.tests — the "related tests" shown per
brief file. Before the fix its JOIN was untyped, so a test connected to the candidate
ONLY via a promoted depth edge (WRITES/READS/DATA_FLOW — sharing written state, not a
CALL) surfaced as a related test. This is the 5th JOIN that was missed when the other
four neighbor surfaces (_static_callees / _issue_relevant_neighbors) were typed to CALLS.

The fix mirrors _static_callees: `AND e.type = 'CALLS'`. A test that genuinely CALLS
the candidate still surfaces; a WRITES-only-connected test does not.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.v1r_brief import _test_files_for


def _graph(tmp_path):
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "file_path TEXT, is_test INTEGER, language TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, resolution_method TEXT, confidence REAL, metadata TEXT)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,?,?)",
        [
            (1, "Function", "target", "src/mod.py", 0, "python"),
            # a real test-CALLER of the candidate -> SHOWN
            (2, "Function", "test_target_calls", "tests/test_mod.py", 1, "python"),
            # a test connected ONLY via a promoted WRITES edge -> EXCLUDED
            (3, "Function", "test_writes_shared_state", "tests/test_other.py", 1, "python"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
        "VALUES (?,?,?,?,?,?)",
        [
            # test_target_calls --CALLS--> target  (test is the caller; edge.target_id=1)
            (2, 1, "CALLS", "import", 1.0, ""),
            # test_writes_shared_state --WRITES--> target's owning state (promoted depth)
            (3, 1, "WRITES", "promote_write", 1.0, "_cache"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_related_tests_are_calls_only(tmp_path):
    db = _graph(tmp_path)
    out = _test_files_for(db, "src/mod.py")
    assert "tests/test_mod.py" in out          # genuine test-CALLER present
    assert "tests/test_other.py" not in out    # WRITES-only test excluded


def test_writes_only_yields_no_related_test(tmp_path):
    """With ONLY a promoted WRITES connection (no CALLS), the surface is empty —
    a test that merely shares written state is never presented as a related test."""
    db = str(tmp_path / "writes_only.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "file_path TEXT, is_test INTEGER, language TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, resolution_method TEXT, confidence REAL, metadata TEXT)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,?,?)",
        [
            (1, "Function", "target", "src/mod.py", 0, "python"),
            (2, "Function", "test_shared", "tests/test_shared.py", 1, "python"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
        "VALUES (2,1,'WRITES','promote_write',1.0,'_state')"
    )
    conn.commit()
    conn.close()
    assert _test_files_for(db, "src/mod.py") == []
