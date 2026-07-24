"""AUDIT 2026-07-24 — the transitive covering-test SELECTION, proven against real SQLite.

MEASURED: covering selection was empty on 28/29 tasks and produced 19 `plan_none` in run
30121930273, so covering_red could never deliver. ROOT CAUSE is structural, not a threshold: the
selection query demanded a DIRECT test -> edited-symbol CALLS edge, but a test almost never calls an
edited leaf helper directly — it calls a PUBLIC API that reaches it. Real coverage is TRANSITIVE, so
a 1-hop query cannot see it no matter how the gates are tuned.

The existing test for this asserts the flag helper's default. That cannot distinguish a working
query from a broken one. This runs BOTH queries against a real graph with the exact shape that
defeats 1-hop, so it fails if the SQL regresses.

Over-selection is SAFE here (unlike an evidence claim): the selected test is then EXECUTED and only
a genuine RED is reported. The graph only shortlists; execution is the ground truth. What must NOT
regress is the deterministic-resolution gate, so a fuzzy edge is asserted to stay excluded.
"""
from __future__ import annotations
import sqlite3

import pytest

_DET = "ast_direct"
_CONF = "AND COALESCE(e.confidence, 0) >= 0.7 "


@pytest.fixture
def graph():
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test INT);
        CREATE TABLE edges(source_id INT, target_id INT, type TEXT,
                           resolution_method TEXT, confidence REAL);
        INSERT INTO nodes VALUES (1,'leaf_helper','pkg/a.py',0),
                                 (2,'public_api','pkg/api.py',0),
                                 (3,'test_public','tests/test_api.py',1);
        -- test -> public_api -> leaf_helper.  Deliberately NO direct test -> leaf edge.
        INSERT INTO edges VALUES (2,1,'CALLS','ast_direct',0.9),
                                 (3,2,'CALLS','ast_direct',0.9);
        """
    )
    return con


def _one_hop(con, target):
    q = ("SELECT DISTINCT nt.name FROM edges e JOIN nodes nt ON nt.id=e.source_id "
         "WHERE e.target_id IN (?) AND e.type='CALLS' AND COALESCE(nt.is_test,0)=1 "
         f"AND LOWER(TRIM(e.resolution_method)) IN ('{_DET}') {_CONF}")
    return con.execute(q, (target,)).fetchall()


def _two_hop(con, target):
    q = ("SELECT DISTINCT nt.name, nt.file_path, MAX(COALESCE(e2.confidence,1.0)) mc "
         "FROM edges e1 JOIN edges e2 ON e2.target_id=e1.source_id "
         "JOIN nodes nm ON nm.id=e1.source_id JOIN nodes nt ON nt.id=e2.source_id "
         "WHERE e1.target_id IN (?) AND e1.type='CALLS' AND e2.type='CALLS' "
         "AND COALESCE(nm.is_test,0)=0 AND COALESCE(nt.is_test,0)=1 "
         f"AND LOWER(TRIM(e1.resolution_method)) IN ('{_DET}') "
         f"AND LOWER(TRIM(e2.resolution_method)) IN ('{_DET}') "
         + _CONF.replace("e.", "e1.") + _CONF.replace("e.", "e2.") +
         "GROUP BY nt.name, nt.file_path ORDER BY mc DESC, nt.name LIMIT 8")
    return con.execute(q, (target,)).fetchall()


def test_one_hop_is_structurally_blind(graph):
    """The measured bug: this emptiness IS the 28/29 dark covering selection."""
    assert _one_hop(graph, 1) == []


def test_two_hop_finds_the_covering_test(graph):
    rows = _two_hop(graph, 1)
    assert [r[0] for r in rows] == ["test_public"], rows
    assert rows[0][1] == "tests/test_api.py"


def test_fuzzy_edges_are_still_excluded(graph):
    """The safety property: over-selection is fine, fuzzy provenance is NOT."""
    graph.execute("UPDATE edges SET resolution_method='fuzzy_name' WHERE source_id=3")
    assert _two_hop(graph, 1) == []
    graph.execute("UPDATE edges SET resolution_method='ast_direct' WHERE source_id=3")
    graph.execute("UPDATE edges SET resolution_method='fuzzy_name' WHERE source_id=2")
    assert _two_hop(graph, 1) == [], "the INNER hop must be gated too, not just the outer"


def test_low_confidence_is_excluded(graph):
    graph.execute("UPDATE edges SET confidence=0.1 WHERE source_id=3")
    assert _two_hop(graph, 1) == []


def test_a_test_in_the_middle_hop_does_not_qualify(graph):
    """The middle hop must be PRODUCTION code (is_test=0) — a test-helper chain is not coverage."""
    graph.execute("UPDATE nodes SET is_test=1 WHERE id=2")
    assert _two_hop(graph, 1) == []
