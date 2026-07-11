"""B-25 + B-26 (authority / correct-or-quiet on the contract pillar).

B-25: `_read_props` fell back to an UNGATED query when a legacy schema lacked the
`confidence` column — laundering unconfidence-able mined values as contract facts.
B-26: `_blast_facts` labeled READS/WRITES 'verified' at conf>=0.5, but the producer
emits undeclared-field edges at 0.6 (below the 0.7 FACT floor) — so 0.6 structural
guesses rendered as 'verified readers'. Also, a legacy schema (no confidence column)
must be quiet, not permissive.

RED before the fixes: B-25 returns the props; B-26 counts the 0.6 reader as verified.
"""
import sqlite3

from groundtruth.pretask.contract_map import _read_props, _blast_facts


# ----------------------------- B-25 -----------------------------
def test_b25_legacy_schema_no_confidence_column_is_quiet():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE properties(node_id INTEGER, kind TEXT, value TEXT, line INTEGER)")
    conn.execute("INSERT INTO properties VALUES(1, 'boundary_condition', 'n > 0', 1)")
    conn.commit()
    # correct-or-quiet: unconfidence-able mined values are NOT deliverable facts
    assert _read_props(conn, [1]) == {}


def test_b25_current_schema_still_gates_by_confidence():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE properties(node_id INTEGER, kind TEXT, value TEXT, line INTEGER, confidence REAL)"
    )
    conn.execute("INSERT INTO properties VALUES(1, 'boundary_condition', 'n > 0', 1, 0.8)")  # pass
    conn.execute("INSERT INTO properties VALUES(1, 'boundary_condition', 'm < 0', 2, 0.3)")  # drop
    conn.commit()
    assert _read_props(conn, [1]) == {"boundary_condition": ["n > 0"]}


# ----------------------------- B-26 -----------------------------
def _blast_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, metadata TEXT, confidence REAL)"
    )
    # edit-target node 1 WRITES field 'cache' on owning class node 10 (verified 0.9)
    conn.execute("INSERT INTO edges(source_id,target_id,type,metadata,confidence) VALUES(1,10,'WRITES','cache',0.9)")
    # two readers of class 10 field 'cache': node 2 verified (0.9), node 3 a 0.6 guess
    conn.execute("INSERT INTO edges(source_id,target_id,type,metadata,confidence) VALUES(2,10,'READS','cache',0.9)")
    conn.execute("INSERT INTO edges(source_id,target_id,type,metadata,confidence) VALUES(3,10,'READS','cache',0.6)")
    conn.commit()
    return conn


def test_b26_060_reader_not_counted_as_verified():
    facts = _blast_facts(_blast_db(), [1], has_conf=True)
    # 0.7 floor: only the 0.9 reader is verified -> "1 verified reader", not "2"
    assert facts == ("writes to cache; 1 verified reader",), facts


def test_b26_legacy_no_confidence_is_quiet():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, metadata TEXT)"
    )
    conn.execute("INSERT INTO edges(source_id,target_id,type,metadata) VALUES(1,10,'WRITES','cache')")
    conn.execute("INSERT INTO edges(source_id,target_id,type,metadata) VALUES(2,10,'READS','cache')")
    conn.commit()
    # no confidence column -> cannot verify -> correct-or-quiet
    assert _blast_facts(conn, [1], has_conf=False) == ()
