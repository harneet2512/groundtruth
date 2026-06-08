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

    Schema carries ``is_test`` (every node is_test=0 here) — _neighbors filters on
    ``n.is_test = 0``, so a node-table without the column makes the query error and
    return [] (the stale-harness cause of the prior failures). source_file/line are
    present for the stdlib-shadow guard SELECT (unused by these edges).
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "label TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL)"
    )
    nodes = [
        (1, "foo", "src/app.py", "Function", 0),
        (2, "dispatch", "src/router.py", "Function", 0),
        (3, "validate", "src/app.py", "Function", 0),
        (4, "parse", "src/http.py", "Function", 0),
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?)", nodes)
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


def test_name_match_above_floor_stripped_when_provenance_known():
    # dispatch --name_match(0.6)--> foo. CURRENT shipped behavior (commit ceee4e94
    # "FACTS-ONLY parity with the witness path"): when the resolution_method column
    # exists, _neighbors gates the SQL to DETERMINISTIC methods only, so a
    # name_match edge is DROPPED entirely (never rendered, not even as
    # "(unverified)"). This is the strictest correct-or-quiet posture — the
    # <gt-graph-map> matches the witness twin edge-for-edge (facts only). The
    # agreement-guard (name_match is NEVER a fact) is preserved a fortiori.
    db = _make_db([(2, 1, 0.6, "name_match")])
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")])
        fm = maps[0]
        assert fm.has_fact is False  # name_match is never promoted to fact
        assert fm.callers == []  # ...and is now stripped by the SQL FACTS-ONLY gate
        assert fm.has_visible is False
        assert cm.render_map(maps) == ""  # nothing to show -> honest abstain
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


def test_facts_shown_namematch_stripped_when_provenance_known():
    # foo calls: validate (same_file, fact) + parse (name_match 0.6).
    # CURRENT shipped behavior (FACTS-ONLY SQL gate, commit ceee4e94): the fact is
    # shown; the name_match callee is stripped at SQL when resolution_method exists.
    db = _make_db([(1, 3, 1.0, "same_file"), (1, 4, 0.6, "name_match")])
    try:
        maps = cm.build_function_map(db, [("src/app.py", "foo")], max_neighbors=5)
        fm = maps[0]
        assert [e.name for e in fm.callees] == ["validate"]  # only the fact
        assert fm.callees[0].is_fact is True
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
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "label TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL)"
    )
    nodes = [
        (1, "foo", "src/app.py", "Function", 0),      # overload A
        (3, "validate", "src/app.py", "Function", 0),  # shared neighbor
        (5, "foo", "src/app.py", "Function", 0),      # overload B (same name+file)
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?)", nodes)
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
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "label TEXT, is_test INTEGER DEFAULT 0)"
    )
    # No confidence column; resolution_method present so we can prove facts survive.
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT)"
    )
    conn.execute("INSERT INTO nodes VALUES (1,'foo','src/app.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (2,'dispatch','src/router.py','Function',0)")  # name_match caller
    conn.execute("INSERT INTO nodes VALUES (3,'validate','src/app.py','Function',0)")     # same_file callee (fact)
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


# ---------------------------------------------------------------------------
# SQUASH-BATCH items #14, #15, #16, #35, #59 — red->green per item.
# ---------------------------------------------------------------------------

import textwrap  # noqa: E402


def _make_db_full(path: str) -> sqlite3.Connection:
    """Open a connection on a full-schema graph.db (nodes carry is_test; edges
    carry source_file/source_line for the stdlib-shadow guard). Caller inserts."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "label TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL)"
    )
    return conn


# --- item #16: _node_ids normalizes path identically to the witness twin ----

def test_item16_node_ids_normalizes_path_separator_and_dot_prefix():
    """#16: a focus path in repo-relative `beets/importer.py` must still match a
    graph that stored a `./`-prefixed OR backslash (Windows-indexed) variant.
    Pre-fix exact `file_path = ?` returned [] -> whole map silently abstained."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    # Stored with a Windows separator AND a ./ prefix — the asymmetric case.
    conn.execute("INSERT INTO nodes VALUES (1,'foo','.\\pkg\\app.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (2,'caller','pkg/router.py','Function',0)")
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (1,2,1,'CALLS',1,NULL,'import',1.0)"
    )
    conn.commit()
    conn.close()
    try:
        # Focus path uses POSIX separators / no prefix — must still resolve.
        ids = []
        c = cm._open_ro(path)
        ids = cm._node_ids(c, "pkg/app.py", "foo")
        c.close()
        assert ids == [1]  # pre-fix: [] (exact match fails on the ./ + backslash)
        maps = cm.build_function_map(path, [("pkg/app.py", "foo")])
        fm = maps[0]
        assert any(e.name == "caller" and e.is_fact for e in fm.callers)
    finally:
        os.unlink(path)


def test_item16_normalize_file_path_matches_witness_twin():
    """#16: the shared normalizer reproduces the witness twin's inline transform
    (replace backslash -> '/', strip leading './' and '/')."""
    assert cm.normalize_file_path("pkg\\app.py") == "pkg/app.py"
    assert cm.normalize_file_path("./pkg/app.py") == "pkg/app.py"
    assert cm.normalize_file_path("/pkg/app.py") == "pkg/app.py"
    assert cm.normalize_file_path("pkg/app.py") == "pkg/app.py"
    assert cm.normalize_file_path("") == ""


# --- item #35: _neighbors applies the stdlib-shadow guard (shared helper) ----

def test_item35_stdlib_shadow_dropped_when_repo_root_given():
    """#35: a DETERMINISTIC-tagged edge that is really `os.walk(` name-matched to a
    same-named PROJECT symbol must be DROPPED in <gt-graph-map> (parity with the
    witness twin's guard). Pre-fix _neighbors had no shadow guard -> rendered bare
    as a fact. Verified by giving repo_root so the call site can be read."""
    import tempfile as _tf
    repo = _tf.mkdtemp()
    # Caller file whose call site is `result = os.walk(top)` — a stdlib attr call.
    caller_rel = "acct/usage.py"
    os.makedirs(os.path.join(repo, "acct"), exist_ok=True)
    with open(os.path.join(repo, caller_rel), "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""\
            def consume(top):
                result = os.walk(top)
                return result
        """))
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    # node 1 = project function `walk` (the false target); node 2 = caller `consume`.
    conn.execute("INSERT INTO nodes VALUES (1,'walk','acct/account.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (2,'consume','acct/usage.py','Function',0)")
    # A DETERMINISTIC (verified_unique) edge — the provenance gate alone trusts it.
    # source_line=2 points at the `os.walk(top)` line in usage.py.
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (1,2,1,'CALLS',2,'acct/usage.py','verified_unique',0.95)"
    )
    conn.commit()
    conn.close()
    try:
        # WITHOUT repo_root: guard cannot read the call site -> edge survives
        # (provenance gate already passed; we never read a half-truth as proof).
        maps_noroot = cm.build_function_map(path, [("acct/account.py", "walk")])
        assert any(e.name == "consume" for e in maps_noroot[0].callers)
        # WITH repo_root: the shadow guard reads `os.walk(` and DROPS the edge.
        maps = cm.build_function_map(path, [("acct/account.py", "walk")], repo_root=repo)
        assert [e.name for e in maps[0].callers] == []  # pre-fix: ['consume']
        assert cm.render_map(maps) == ""  # nothing real -> abstain
    finally:
        os.unlink(path)


def test_item35_real_deterministic_caller_survives_guard():
    """#35 negative control: a genuine cross-file caller (NOT a stdlib attr call)
    must STILL be shown when repo_root is given — the guard is surgical, not a nuke."""
    import tempfile as _tf
    repo = _tf.mkdtemp()
    os.makedirs(os.path.join(repo, "pkg"), exist_ok=True)
    with open(os.path.join(repo, "pkg/router.py"), "w", encoding="utf-8") as fh:
        fh.write("def dispatch():\n    return foo()\n")  # real local call, no stdlib head
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    conn.execute("INSERT INTO nodes VALUES (1,'foo','pkg/app.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (2,'dispatch','pkg/router.py','Function',0)")
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (1,2,1,'CALLS',2,'pkg/router.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    try:
        maps = cm.build_function_map(path, [("pkg/app.py", "foo")], repo_root=repo)
        assert any(e.name == "dispatch" and e.is_fact for e in maps[0].callers)
    finally:
        os.unlink(path)


# --- item #14: verified_caller_count is an UNCAPPED count -------------------

def test_item14_verified_caller_count_not_truncated_by_display_cap():
    """#14: a function with more verified callers than the legacy max_neighbors=5
    display cap must report the TRUE count. Pre-fix counted the callers of a
    build_function_map(dynamic=False) result, truncated at 5 -> 30 callers read 5."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    conn.execute("INSERT INTO nodes VALUES (1,'hub','pkg/core.py','Function',0)")
    n_callers = 12  # > legacy cap of 5
    for cid in range(2, 2 + n_callers):
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,0)",
            (cid, f"caller{cid}", f"pkg/m{cid}.py", "Function"),
        )
        conn.execute(
            "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES (?,?,1,'CALLS',1,NULL,'import',1.0)",
            (cid, cid),
        )
    # plus one name_match caller that must NEVER be counted
    conn.execute("INSERT INTO nodes VALUES (99,'guess','pkg/x.py','Function',0)")
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (99,99,1,'CALLS',1,NULL,'name_match',0.9)"
    )
    conn.commit()
    conn.close()
    try:
        cnt = cm.verified_caller_count(path, "pkg/core.py", "hub")
        assert cnt == n_callers  # pre-fix: capped at 5; name_match never counted
    finally:
        os.unlink(path)


def test_item14_contract_map_delegates_to_uncapped_count():
    """#14: contract_map._verified_caller_count now delegates to the uncapped
    primitive, so the drift block's "N verified callers" is no longer truncated."""
    from groundtruth.pretask import contract_map as ctm
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    conn.execute("INSERT INTO nodes VALUES (1,'hub','pkg/core.py','Function',0)")
    for cid in range(2, 11):  # 9 verified callers (> cap of 5)
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,0)",
            (cid, f"caller{cid}", f"pkg/m{cid}.py", "Function"),
        )
        conn.execute(
            "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES (?,?,1,'CALLS',1,NULL,'same_file',1.0)",
            (cid, cid),
        )
    conn.commit()
    conn.close()
    try:
        assert ctm._verified_caller_count(path, "pkg/core.py", "hub") == 9
    finally:
        os.unlink(path)


# --- item #15: 2-hop rescue gated on FACT count, not total visible ----------

def test_item15_rescue_fires_when_zero_facts_despite_namematch_noise():
    """#15: a focus with 0 FACT 1-hop edges must trigger the verified 2-hop rescue
    even if name_match 'noise' is present. Pre-fix the sparseness test counted total
    visible edges, so guesses suppressed the rescue on exactly the isolated targets.

    Because the SQL FACTS-ONLY gate already strips name_match from the visible set,
    the regression is exercised at the predicate level: with 0 facts the rescue MUST
    run. Layout: foo --same_file--> mid (1 fact, sparse); mid --import--> deep (a
    verified 2-hop fact). foo's own 1-hop fact count is 1 (== threshold) so rescue
    fires and surfaces `deep` at hops=2."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    conn.execute("INSERT INTO nodes VALUES (1,'foo','pkg/a.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (2,'mid','pkg/b.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (3,'deep','pkg/c.py','Function',0)")
    # foo --calls--> mid  (1-hop fact); mid --calls--> deep (2-hop fact)
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (1,1,2,'CALLS',1,NULL,'same_file',1.0)"
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (2,2,3,'CALLS',1,NULL,'import',1.0)"
    )
    conn.commit()
    conn.close()
    try:
        maps = cm.build_function_map(path, [("pkg/a.py", "foo")])
        callee_names = {e.name: e for e in maps[0].callees}
        assert "mid" in callee_names  # 1-hop fact
        assert "deep" in callee_names  # rescued via verified 2-hop
        assert callee_names["deep"].hops == 2
    finally:
        os.unlink(path)


def test_item15_dynamic_neighbors_sparseness_uses_fact_count(monkeypatch):
    """#15 (predicate-level, white-box): _dynamic_neighbors must gate the rescue on
    the FACT count, NOT total visible. We feed a 1-hop set of {1 fact + 2 unverified}
    — total 3, facts 1. Old predicate (len(edges)=3 > 1) SKIPS the rescue; the fix
    (len(fact_neighbors)=1, not > 1) lets it run. We stub the SQL/budget layer so an
    unverified edge can coexist with a fact in ``edges`` (which the production
    FACTS-ONLY SQL gate normally prevents), isolating the predicate under test.

    Stub: first 1-hop call returns the mixed set; _apply_dynamic_budget passthrough;
    _node_ids (seed expansion) + _second_hop_facts assert the rescue actually fired.
    """
    fact = cm.Edge(name="seed", file="b.py", confidence=1.0, resolution_method="import")
    g1 = cm.Edge(name="g1", file="c.py", confidence=0.6, resolution_method="name_match")
    g2 = cm.Edge(name="g2", file="d.py", confidence=0.6, resolution_method="name_match")

    rescued = {"fired": False}

    def fake_neighbors(conn, ids, **kw):
        return [fact, g1, g2]  # 1 fact + 2 unverified -> total 3, facts 1

    def fake_budget(edges, **kw):
        return list(edges)  # passthrough so the mixed set reaches the predicate

    def fake_node_ids(conn, file, name):
        return [42] if name == "seed" else []

    def fake_second_hop(conn, seed_ids, **kw):
        rescued["fired"] = True
        return [cm.Edge(name="deep", file="z.py", confidence=1.0,
                        resolution_method="import", hops=2)]

    monkeypatch.setattr(cm, "_neighbors", fake_neighbors)
    monkeypatch.setattr(cm, "_apply_dynamic_budget", fake_budget)
    monkeypatch.setattr(cm, "_node_ids", fake_node_ids)
    monkeypatch.setattr(cm, "_verified_neighbor_count", lambda *a, **k: 1)
    monkeypatch.setattr(cm, "_second_hop_facts", fake_second_hop)

    # conn.execute is only used to add the focus's own (name,file) to exclude.
    class _Cur:
        def fetchone(self):
            return ("foo", "a.py")

    class _Conn:
        def execute(self, *a, **k):
            return _Cur()

    out = cm._dynamic_neighbors(
        _Conn(), [1], direction="callees", has_conf=True, has_method=True,
        fact_ceiling=8, unverified_k=3, second_hop=True,
    )
    # FACT count is 1 (== threshold, not >), so the rescue MUST fire and add `deep`.
    assert rescued["fired"] is True  # pre-fix: predicate len(edges)=3>1 -> never fired
    assert any(e.name == "deep" and e.hops == 2 for e in out)


# --- item #59: true COUNT for budget; rescue over-fetch past exclude --------

def test_item59_budget_uses_true_fact_count_on_a_big_hub():
    """#59: on a hub with more verified neighbors than the over-fetch window, the
    unverified-guess budget must shrink against the TRUE (uncapped) fact count, not
    the windowed count — a fact-rich hub shows ZERO guesses. We assert there are
    many facts and that _verified_neighbor_count returns the true (uncapped) number,
    which _apply_dynamic_budget consumes to zero out the guess budget."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    conn.execute("INSERT INTO nodes VALUES (1,'hub','pkg/core.py','Function',0)")
    n = 25  # > fact_ceiling(8)+unverified_k(3)+8 = 19 over-fetch window
    for cid in range(2, 2 + n):
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,0)",
            (cid, f"c{cid}", f"pkg/m{cid}.py", "Function"),
        )
        conn.execute(
            "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES (?,?,1,'CALLS',1,NULL,'import',1.0)",
            (cid, cid),
        )
    conn.commit()
    conn.close()
    try:
        c = cm._open_ro(path)
        _, has_method = cm._has_columns(c)
        ids = cm._node_ids(c, "pkg/core.py", "hub")
        true_facts = cm._verified_neighbor_count(c, ids, direction="callers", has_method=has_method)
        c.close()
        assert true_facts == n  # uncapped, not the 19-row window
        # End-to-end: budget consumes the true count -> guess budget is 0; every
        # shown caller is a fact (no unverified leakage on a fact-rich hub).
        maps = cm.build_function_map(path, [("pkg/core.py", "hub")])
        assert all(e.is_fact for e in maps[0].callers)
        assert len(maps[0].callers) <= cm._FACT_CEILING  # facts capped, not guesses added
    finally:
        os.unlink(path)


def test_item59_apply_dynamic_budget_true_count_zeros_guess_budget():
    """#59 (unit): with a TRUE fact count exceeding unverified_k, the guess budget
    is zero even if the windowed `edges` under-counts facts (the mega-hub case)."""
    facts = [cm.Edge(name=f"f{i}", file="a.py", confidence=1.0,
                     resolution_method="import") for i in range(2)]
    guesses = [cm.Edge(name="g1", file="b.py", confidence=0.6,
                       resolution_method="name_match")]
    # Windowed edges show only 2 facts, but the TRUE count is 10 -> guesses dropped.
    out = cm._apply_dynamic_budget(
        facts + guesses, fact_ceiling=8, unverified_k=3, true_fact_count=10
    )
    assert all(e.is_fact for e in out)
    assert all(e.resolution_method != "name_match" for e in out)


def test_item59_second_hop_overfetch_survives_heavy_exclude():
    """#59: the 2-hop rescue over-fetches by len(exclude). A well-connected seed
    whose top-sorted neighbors are ALL already-shown (excluded) must still yield a
    valid rescue instead of truncating-before-excluding to zero.

    Construction: the seed has MANY excluded siblings (named to sort BEFORE the
    genuinely-new node) so the pre-fix small window (limit*4 = 12) is entirely
    consumed by excluded rows and the new node `zzz_deepnew` (sorts LAST) falls
    outside it. With the fix (window += len(exclude)) the new node survives."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _make_db_full(path)
    conn.execute("INSERT INTO nodes VALUES (1,'foo','pkg/a.py','Function',0)")
    conn.execute("INSERT INTO nodes VALUES (2,'seed','pkg/b.py','Function',0)")
    conn.execute("INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
                 "resolution_method,confidence) VALUES (1,1,2,'CALLS',1,NULL,'same_file',1.0)")
    eid = 2
    # 20 sibs (> limit*4=12) named 'sib00'.. so they sort before 'zzz_deepnew'.
    n_sibs = 20
    sib_keys = []
    for i in range(n_sibs):
        sid = 10 + i
        sname = f"sib{i:02d}"
        sfile = f"pkg/s{i:02d}.py"
        sib_keys.append((sname, sfile))
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,0)", (sid, sname, sfile, "Function"))
        conn.execute("INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
                     "resolution_method,confidence) VALUES (?,2,?,'CALLS',1,NULL,'import',1.0)",
                     (eid, sid)); eid += 1
    # genuinely-new 2-hop target, named to sort LAST (z...), reachable only from seed
    conn.execute("INSERT INTO nodes VALUES (99,'zzz_deepnew','pkg/z.py','Function',0)")
    conn.execute("INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
                 "resolution_method,confidence) VALUES (?,2,99,'CALLS',1,NULL,'import',1.0)", (eid,))
    conn.commit()
    conn.close()
    try:
        c = cm._open_ro(path)
        has_conf, has_method = cm._has_columns(c)
        seed_ids = cm._node_ids(c, "pkg/b.py", "seed")
        exclude = set(sib_keys)  # all 20 sibs already shown at hop1 -> excluded
        exclude.add(("foo", "pkg/a.py"))
        hop2 = cm._second_hop_facts(
            c, seed_ids, direction="callees", has_conf=has_conf,
            has_method=has_method, exclude=exclude, limit=3,
        )
        c.close()
        names = {e.name for e in hop2}
        assert "zzz_deepnew" in names  # pre-fix: truncated away by the limit*4 window
        assert all(e.hops == 2 for e in hop2)
    finally:
        os.unlink(path)
