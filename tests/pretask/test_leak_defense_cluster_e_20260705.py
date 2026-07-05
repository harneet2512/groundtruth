"""Cluster E (Fable 2026-07-05): leak defense-in-depth on FROZEN graphs.

is_test=0 (a SQL flag) is not a leak guard on its own — a stale/frozen graph can carry a
test/demo/vendored node whose is_test flag was never set. E1 routes the brief's `Calls:`
neighbor surface through the Class-A path chokepoint (path_policy.is_deliverable); E2 adds a
test-PATH admission guard to witness rendering and catches pytest/unittest test SYMBOL names.

Each test RED-proofs by exercising the exact leak the guard closes.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.v1r_brief import _issue_relevant_neighbors, _static_callees
from groundtruth.pretask.graph_localizer import Candidate, Witness, _is_test_block_name


# --------------------------------------------------------------------------- E1

def _graph_with_test_neighbor(tmp_path) -> str:
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
            (1, "Function", "fa", "src/a.py", 0, "python"),
            (2, "Function", "fb", "lib/real.py", 0, "python"),          # deliverable → kept
            # A test-path neighbor whose is_test flag was NEVER set (frozen-graph shape):
            (3, "Function", "helper", "tests/conftest.py", 0, "python"),  # test path → dropped
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) VALUES (?,?,?,?,?)",
        [
            (1, 2, "CALLS", "import", 1.0),
            (1, 3, "CALLS", "import", 1.0),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_static_callees_drops_test_path_neighbor(tmp_path):
    out = _static_callees(_graph_with_test_neighbor(tmp_path), "src/a.py")
    assert "lib/real.py" in out                 # deliverable source neighbor kept
    assert "tests/conftest.py" not in out       # E1 RED without the is_deliverable filter


def test_issue_relevant_neighbors_drops_test_path_neighbor(tmp_path):
    out = _issue_relevant_neighbors(
        _graph_with_test_neighbor(tmp_path), "src/a.py", str(tmp_path), {"anything"}
    )
    assert "tests/conftest.py" not in out       # E1 RED without the is_deliverable filter


# --------------------------------------------------------------------------- E2

def test_is_test_block_name_catches_pytest_unittest_symbol_names():
    # RED before E2: these return False (only mocha/jest block names were caught).
    assert _is_test_block_name("test_foo") is True
    assert _is_test_block_name("foo_test") is True
    assert _is_test_block_name("TestClient") is True
    # correct-or-quiet must NOT over-suppress real code symbols:
    assert _is_test_block_name("set_fields") is False
    assert _is_test_block_name("Testing") is False     # ^Test[A-Z] only, not "Testing"
    assert _is_test_block_name("handler") is False


def _candidate_with_test_path_witness() -> Candidate:
    # Benign SYMBOL names (not caught by the name guard) but a test-PATH file — the exact
    # shape E2's admission guard closes: a real code symbol that merely lives in tests/.
    w = Witness(
        file_path="tests/test_x.py",
        anchor="do_work",
        edge_type="CALLS",
        direction="calls_anchor",
        verified=True,
        confidence=1.0,
        hop=0,
        src_symbol="do_work",
        dst_symbol="compute",
    )
    return Candidate(file_path="tests/test_x.py", score=1.0, witnesses=[w],
                     lex_hits=0, degree=1, confidence=1.0)


def test_render_witness_drops_test_path_witness():
    # RED before E2 part 2: renders "do_work ... compute" (names pass the name guard).
    assert _candidate_with_test_path_witness().render_witness() == ""
