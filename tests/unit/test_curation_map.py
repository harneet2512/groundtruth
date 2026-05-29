"""Curation map — 1-hop callers/callees, correct-or-quiet tiering.

Proves the mechanism, not just that code runs:
- a deterministic edge renders as a FACT;
- a name_match edge above the floor renders marked (unverified);
- a name_match edge below the floor is SUPPRESSED entirely;
- the agreement-guard: a name_match edge is NEVER a fact;
- honest abstention: no confident connection -> empty render / any_signal False.
"""
import os
import sqlite3
import tempfile

from groundtruth.pretask import curation_map as cm


def _make_db(edges: list[tuple[int, int, float, str]]) -> str:
    """Build a tiny graph.db. Nodes: 1=target foo (src/app.py),
    2=caller dispatch (src/router.py), 3=callee validate (src/app.py),
    4=callee parse (src/http.py). edges = (source_id, target_id, confidence, method).
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, label TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL)"
    )
    nodes = [
        (1, "foo", "src/app.py", "Function"),
        (2, "dispatch", "src/router.py", "Function"),
        (3, "validate", "src/app.py", "Function"),
        (4, "parse", "src/http.py", "Function"),
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?)", nodes)
    for i, (src, tgt, conf, method) in enumerate(edges, start=1):
        conn.execute(
            "INSERT INTO edges (id, source_id, target_id, type, source_line, "
            "source_file, resolution_method, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (i, src, tgt, "CALLS", 1, None, method, conf),
        )
    conn.commit()
    conn.close()
    return path


def test_deterministic_edge_is_a_fact():
    # dispatch --import--> foo  (verified caller); foo --same_file--> validate (callee)
    db = _make_db([(2, 1, 1.0, "import"), (1, 3, 1.0, "same_file")])
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")])
        assert len(maps) == 1
        fm = maps[0]
        assert fm.has_fact
        caller_names = {e.name: e.is_fact for e in fm.callers}
        callee_names = {e.name: e.is_fact for e in fm.callees}
        assert caller_names.get("dispatch") is True
        assert callee_names.get("validate") is True
        rendered = cm.render_map(maps)
        assert "dispatch (src/router.py)" in rendered
        assert "validate (src/app.py)" in rendered
        assert "(unverified)" not in rendered  # facts are not marked
    finally:
        os.unlink(db)


def test_name_match_above_floor_marked_unverified_never_a_fact():
    # dispatch --name_match(0.6)--> foo : visible but NOT a fact (agreement-guard)
    db = _make_db([(2, 1, 0.6, "name_match")])
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")])
        fm = maps[0]
        assert fm.has_visible is True
        assert fm.has_fact is False  # name_match is never promoted to fact
        e = fm.callers[0]
        assert e.name == "dispatch"
        assert e.is_fact is False
        rendered = cm.render_map(maps)
        assert "dispatch (src/router.py) (unverified)" in rendered
    finally:
        os.unlink(db)


def test_name_match_below_floor_suppressed():
    # dispatch --name_match(0.2)--> foo : below floor -> not rendered at all
    db = _make_db([(2, 1, 0.2, "name_match")])
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")])
        fm = maps[0]
        assert fm.callers == []
        assert fm.has_visible is False
        assert cm.render_map(maps) == ""  # nothing confident -> abstain (empty)
        assert cm.any_signal(maps) is False
    finally:
        os.unlink(db)


def test_no_connections_abstains():
    db = _make_db([])  # foo has no edges
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")])
        assert maps[0].has_visible is False
        assert cm.render_map(maps) == ""
        assert cm.any_signal(maps) is False
    finally:
        os.unlink(db)


def test_facts_ordered_before_unverified_and_capped():
    # foo calls: validate (same_file, fact) + parse (name_match 0.6, unverified)
    db = _make_db([(1, 3, 1.0, "same_file"), (1, 4, 0.6, "name_match")])
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")], max_neighbors=5)
        fm = maps[0]
        assert [e.name for e in fm.callees] == ["validate", "parse"]  # fact first
        assert fm.callees[0].is_fact is True
        assert fm.callees[1].is_fact is False
    finally:
        os.unlink(db)


def test_overload_same_neighbor_keeps_fact_not_name_match():
    """Finding 1 (HIGH): when a focus name resolves to >1 node id and reaches the
    SAME neighbor via a same_file (fact) edge AND a name_match edge, the kept Edge
    must be the FACT regardless of SQL row order — no silent downgrade.

    Layout: two foo definitions in src/app.py (ids 1 and 5; _node_ids unions both).
    Both call neighbor `validate` (id 3): id 5 via name_match (edge inserted FIRST,
    so it leads under DISTINCT's natural row order), id 1 via same_file (fact).
    The pre-fix dedup keeps the first-seen row -> name_match -> '(unverified)'.
    Post-fix: the fact wins.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, label TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL)"
    )
    nodes = [
        (1, "foo", "src/app.py", "Function"),      # overload A
        (3, "validate", "src/app.py", "Function"),  # shared neighbor
        (5, "foo", "src/app.py", "Function"),      # overload B (same name+file)
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?)", nodes)
    # name_match edge inserted FIRST so it precedes the fact row in natural order.
    conn.execute(
        "INSERT INTO edges (id, source_id, target_id, type, source_line, "
        "source_file, resolution_method, confidence) VALUES (1,5,3,'CALLS',1,NULL,'name_match',0.6)"
    )
    conn.execute(
        "INSERT INTO edges (id, source_id, target_id, type, source_line, "
        "source_file, resolution_method, confidence) VALUES (2,1,3,'CALLS',1,NULL,'same_file',1.0)"
    )
    conn.commit()
    conn.close()
    try:
        maps = cm.build_function_map(path, [("src/app.py", "foo")])
        fm = maps[0]
        # exactly one deduped callee, and it must be the FACT
        validate_edges = [e for e in fm.callees if e.name == "validate"]
        assert len(validate_edges) == 1
        assert validate_edges[0].is_fact is True
        rendered = cm.render_map(maps)
        assert "validate (src/app.py)" in rendered
        assert "validate (src/app.py) (unverified)" not in rendered
    finally:
        os.unlink(path)


def test_open_ro_closes_connection_when_pragma_raises(monkeypatch):
    """Finding 4 (LOW): if a PRAGMA raises after connect() succeeded, _open_ro
    must close the half-open handle before returning None — no leaked connection.
    """
    closed = {"called": False}

    class FakeConn:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("pragma boom")

        def close(self):
            closed["called"] = True

    monkeypatch.setattr(cm.sqlite3, "connect", lambda *a, **k: FakeConn())
    result = cm._open_ro("whatever.db")
    assert result is None
    assert closed["called"] is True


def test_missing_db_returns_empty():
    assert cm.build_function_map("/no/such/path.db", [("a.py", "f")]) == []


def test_db_without_confidence_columns_suppresses_nonfacts_keeps_facts():
    """Finding 5 (LOW): with no confidence column we must NOT synthesize a
    floor-clearing value. A name_match/unknown-provenance edge with unknown
    confidence is treated as below-floor and SUPPRESSED (correct-or-quiet); only
    a deterministic-method edge (a FACT, which ignores confidence) stays visible.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, label TEXT)")
    # No confidence column; resolution_method present so we can prove facts survive.
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, resolution_method TEXT)"
    )
    conn.execute("INSERT INTO nodes VALUES (1,'foo','src/app.py','Function')")
    conn.execute("INSERT INTO nodes VALUES (2,'dispatch','src/router.py','Function')")  # name_match caller
    conn.execute("INSERT INTO nodes VALUES (3,'validate','src/app.py','Function')")     # same_file callee (fact)
    conn.execute(
        "INSERT INTO edges (id, source_id, target_id, type, resolution_method) "
        "VALUES (1,2,1,'CALLS','name_match')"
    )
    conn.execute(
        "INSERT INTO edges (id, source_id, target_id, type, resolution_method) "
        "VALUES (2,1,3,'CALLS','same_file')"
    )
    conn.commit()
    conn.close()
    try:
        maps = cm.build_function_map(path, [("src/app.py", "foo")])
        fm = maps[0]
        # name_match caller with unknown confidence -> below floor -> suppressed.
        assert [e.name for e in fm.callers] == []
        # deterministic same_file callee -> still a FACT, still visible.
        assert [e.name for e in fm.callees] == ["validate"]
        assert fm.callees[0].is_fact is True
        rendered = cm.render_map(maps)
        assert "validate (src/app.py)" in rendered
        assert "(unverified)" not in rendered          # nothing rendered unverified
        assert "dispatch" not in rendered              # name_match caller suppressed
    finally:
        os.unlink(path)


def test_db_no_method_no_conf_columns_fully_suppressed():
    """Oldest schema: neither resolution_method nor confidence columns. Every
    edge is unknown-provenance with unknown confidence -> all suppressed (quiet
    when uncertain), so the map abstains rather than rendering bare guesses.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, label TEXT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT)")
    conn.execute("INSERT INTO nodes VALUES (1,'foo','src/app.py','Function')")
    conn.execute("INSERT INTO nodes VALUES (2,'dispatch','src/router.py','Function')")
    conn.execute("INSERT INTO edges (id, source_id, target_id, type) VALUES (1,2,1,'CALLS')")
    conn.commit()
    conn.close()
    try:
        maps = cm.build_function_map(path, [("src/app.py", "foo")])
        fm = maps[0]
        assert fm.has_visible is False
        assert cm.render_map(maps) == ""
        assert cm.any_signal(maps) is False
    finally:
        os.unlink(path)
