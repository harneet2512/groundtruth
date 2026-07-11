"""Wave-1 consumer wiring in the Gateway (B-11 · B-23 · B-24 · B-12 · B-21 · B-29).

Each test names the invariant. The Go indexer now stores the facts these consumers read
(project_meta.post_revision + subrev_* · content_passages(+_fts) · edge_metadata); the
gateway must READ them (WAL-visible / passage-cited / normalized) and ENFORCE the
envelope's timing + freshness — with back-compat on an old graph.db.
"""
from __future__ import annotations

import sqlite3

from groundtruth.runtime import gateway as gw
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _mk_db(tmp_path, *, meta=None, nodes=(), edges=(), passages=None,
           edge_metadata=None, symbol_fts=None):
    """Build a graph.db on disk with the requested surfaces. Only the tables a test needs
    are created, so a test can exercise the OLD-schema fallback by omitting a surface."""
    path = str(tmp_path / "graph.db")
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5),
             n.get("is_test", 0), n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
            "confidence,metadata) VALUES(?,?,?,?,?,?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("source_line", 1), e.get("resolution_method", "import"),
             e.get("confidence", 1.0), e.get("metadata")))
    if meta is not None:
        con.execute("CREATE TABLE project_meta(key TEXT PRIMARY KEY, value TEXT)")
        for k, v in meta.items():
            con.execute("INSERT INTO project_meta VALUES(?,?)", (k, v))
    if edge_metadata is not None:
        con.execute("CREATE TABLE edge_metadata(edge_id INTEGER, key TEXT, value TEXT,"
                    " schema_version INTEGER, PRIMARY KEY(edge_id,key))")
        for eid, key, val in edge_metadata:
            con.execute("INSERT INTO edge_metadata VALUES(?,?,?,1)", (eid, key, val))
    if symbol_fts is not None:
        con.execute("CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content)")
        for rowid, content in symbol_fts:
            con.execute("INSERT INTO symbol_content_fts(rowid,content) VALUES(?,?)", (rowid, content))
    if passages is not None:
        con.execute("CREATE TABLE content_passages(passage_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " node_id INTEGER, start_line INTEGER, end_line INTEGER, content TEXT,"
                    " content_hash TEXT)")
        con.execute("CREATE VIRTUAL TABLE content_passages_fts USING fts5(content)")
        for node_id, sl, el, content in passages:
            cur = con.execute(
                "INSERT INTO content_passages(node_id,start_line,end_line,content,content_hash)"
                " VALUES(?,?,?,?,?)", (node_id, sl, el, content, "h" + str(sl)))
            con.execute("INSERT INTO content_passages_fts(rowid,content) VALUES(?,?)",
                        (cur.lastrowid, content))
    con.commit()
    con.close()
    return path


# =========================================================================== #
# B-11 — graph_revision reads project_meta.post_revision LIVE (not a file hash)
# =========================================================================== #
def test_b11_reads_post_revision_and_tracks_live(tmp_path):
    path = _mk_db(tmp_path, meta={"post_revision": "REV_A"})
    st = gw.GatewayState(graph_db=path)
    assert gw._graph_revision(st) == "REV_A"       # the logical revision, NOT a file hash
    # a committed reindex bumps post_revision -> the LIVE query tracks it (not a stale cache)
    con = sqlite3.connect(path)
    con.execute("UPDATE project_meta SET value='REV_B' WHERE key='post_revision'")
    con.commit()
    con.close()
    assert gw._graph_revision(st) == "REV_B"


def test_b11_legacy_db_without_project_meta_falls_back_to_file_hash(tmp_path):
    path = _mk_db(tmp_path)  # no project_meta
    st = gw.GatewayState(graph_db=path)
    rev = gw._graph_revision(st)
    assert rev and len(rev) == 12 and all(c in "0123456789abcdef" for c in rev)


# =========================================================================== #
# B-29 — per-fact-class freshness over subrev_<surface> (not the whole composite)
# =========================================================================== #
def test_b29_valid_until_is_per_fact_class(tmp_path):
    meta = {"post_revision": "COMPOSITE", "subrev_nodes": "N1", "subrev_edges": "E1",
            "subrev_content_fts": "C1", "subrev_closure": "X1"}
    path = _mk_db(tmp_path, meta=meta)
    st = gw.GatewayState(graph_db=path)
    gr_def, vu_def = gw._revisions_for(st, "def_ref_partition")   # nodes+edges
    gr_body, vu_body = gw._revisions_for(st, "body_concept")      # nodes+edges+content_fts
    assert gr_def == gr_body == "COMPOSITE"        # B-11 composite for both
    assert vu_def != vu_body                        # body_concept ALSO depends on content_fts
    assert vu_def != "COMPOSITE" and vu_body != "COMPOSITE"  # narrowed, not the whole composite


def test_b29_content_edit_invalidates_only_dependent_class(tmp_path):
    meta = {"post_revision": "COMPOSITE", "subrev_nodes": "N1", "subrev_edges": "E1",
            "subrev_content_fts": "C1"}
    path = _mk_db(tmp_path, meta=meta)
    st = gw.GatewayState(graph_db=path)
    vu_def_before = gw._revisions_for(st, "def_ref_partition")[1]
    vu_body_before = gw._revisions_for(st, "body_concept")[1]
    # a body-only edit moves ONLY the content_fts sub-rev
    con = sqlite3.connect(path)
    con.execute("UPDATE project_meta SET value='C2' WHERE key='subrev_content_fts'")
    con.commit()
    con.close()
    assert gw._revisions_for(st, "def_ref_partition")[1] == vu_def_before   # UNCHANGED
    assert gw._revisions_for(st, "body_concept")[1] != vu_body_before        # INVALIDATED


def test_b29_legacy_db_valid_until_equals_graph_revision(tmp_path):
    """No sub-revs -> valid_until falls back to graph_revision (legacy invariant preserved)."""
    path = _mk_db(tmp_path, meta={"post_revision": "REV_A"})  # no subrev_* keys
    st = gw.GatewayState(graph_db=path)
    gr, vu = gw._revisions_for(st, "def_ref_partition")
    assert gr == vu == "REV_A"


# =========================================================================== #
# B-12 — timing + freshness enforced as REAL routing
# =========================================================================== #
def _env(producer="def_ref_partition", preferred_event="search", valid_until=""):
    return EvidenceEnvelope.build(
        producer=producer, fact_id="run", target="a/x.py", evidence_type="def_ref_partition",
        payload=("def: a/x.py:1",), provenance=(("a/x.py", 1),), confidence=0.9,
        tier="VERIFIED", graph_revision="g", valid_until=valid_until,
        preferred_event=preferred_event,
    )


def test_b12_on_time_delivers(tmp_path):
    st = gw.GatewayState(graph_db=_mk_db(tmp_path))
    assert gw.route_delivery(_env(preferred_event="search"),
                             gw.ToolEvent(kind="search"), st) == gw.ROUTE_DELIVER


def test_b12_too_early_defers(tmp_path):
    st = gw.GatewayState(graph_db=_mk_db(tmp_path))
    # fact's boundary is 'test'; the current event is a 'search' (earlier) -> defer
    assert gw.route_delivery(_env(preferred_event="test"),
                             gw.ToolEvent(kind="search"), st) == gw.ROUTE_DEFER


def test_b12_too_late_expires(tmp_path):
    st = gw.GatewayState(graph_db=_mk_db(tmp_path))
    # fact's boundary is 'edit'; the current event is a 'submit' (later) -> expired_late
    assert gw.route_delivery(_env(preferred_event="edit"),
                             gw.ToolEvent(kind="submit"), st) == gw.ROUTE_EXPIRED_LATE


def test_b12_stale_freshness_discards(tmp_path):
    meta = {"post_revision": "COMPOSITE", "subrev_nodes": "N1", "subrev_edges": "E1"}
    st = gw.GatewayState(graph_db=_mk_db(tmp_path, meta=meta))
    # a valid_until that does NOT match the live per-surface composite -> stale (a depended
    # sub-rev changed since the fact was built) -> never delivered, even on-time.
    stale = _env(preferred_event="search", valid_until="deadbeefdeadbeef")
    assert gw.route_delivery(stale, gw.ToolEvent(kind="search"), st) == gw.ROUTE_STALE


def test_b12_augment_drops_non_deliver(tmp_path, monkeypatch):
    """augment ENFORCES the verdict: a fact routed to anything but ROUTE_DELIVER ships 0
    bytes (an expired_late fact is an explicit non-delivery, not a success)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    path = _mk_db(tmp_path, nodes=[
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    st = gw.GatewayState(graph_db=path, repo_root=str(tmp_path))
    ev = gw.ToolEvent(kind="search", command="grep -rn run .",
                      output="a/x.py:10: run\nb/y.py:20: run")
    assert gw.augment(ev, st)   # happy path delivers the def/ref partition
    monkeypatch.setattr(gw, "route_delivery", lambda *a, **k: gw.ROUTE_EXPIRED_LATE)
    st2 = gw.GatewayState(graph_db=path, repo_root=str(tmp_path))
    assert gw.augment(ev, st2) == []   # routing verdict enforced -> 0 bytes


# =========================================================================== #
# B-21 — the per-(kind,file,graph_revision) delivery-latch key
# =========================================================================== #
def test_b21_latch_key_revision_scoped():
    a = gw.delivery_latch_key("view", "src/a.py", "rev1")
    assert gw.delivery_latch_key("view", "src/a.py", "rev1") == a       # deterministic
    assert gw.delivery_latch_key("view", "src/a.py", "rev2") != a       # rev change re-permits
    assert gw.delivery_latch_key("edit", "src/a.py", "rev1") != a       # kind matters
    assert gw.delivery_latch_key("view", "src/b.py", "rev1") != a       # file matters
    # path-normalized: ./ prefix and backslashes fold to the same key
    assert gw.delivery_latch_key("view", "./src/a.py", "rev1") == a
    assert gw.delivery_latch_key("view", "src\\a.py", "rev1") == a


# =========================================================================== #
# B-23 — body-concept cites the EXACT matching passage line, not node start_line
# =========================================================================== #
def test_b23_cites_matching_passage_line(tmp_path):
    path = _mk_db(
        tmp_path,
        nodes=[{"id": 1, "name": "handler", "file_path": "svc/h.py", "start_line": 10, "end_line": 60}],
        symbol_fts=[(1, "redis tls handshake fails config")],
        passages=[(1, 11, 11, "setup init config"), (1, 42, 42, "handshake fails here")],
    )
    con = gw._connect_ro(path)
    try:
        rows = gw._body_rows(con, "handshake", "")
    finally:
        con.close()
    assert rows
    rel, line, name, label = rows[0]
    assert rel == "svc/h.py"
    assert line == 42          # the passage line, NOT the node start_line 10


def test_b23_fallback_cites_node_start_line_on_old_db(tmp_path):
    path = _mk_db(
        tmp_path,
        nodes=[{"id": 1, "name": "handler", "file_path": "svc/h.py", "start_line": 10}],
        symbol_fts=[(1, "redis tls handshake fails")],
        # NO content_passages tables (old db)
    )
    con = gw._connect_ro(path)
    try:
        rows = gw._body_rows(con, "handshake", "")
    finally:
        con.close()
    assert rows and rows[0][1] == 10   # falls back to node start_line


# =========================================================================== #
# B-24 — receiver_type read from the normalized edge_metadata sub-table
# =========================================================================== #
def test_b24_fact_callers_receiver_from_edge_metadata(tmp_path):
    path = _mk_db(
        tmp_path,
        nodes=[{"id": 1, "name": "save", "file_path": "db.py", "start_line": 1},
               {"id": 2, "name": "handler", "file_path": "h.py", "start_line": 1}],
        edges=[{"id": 10, "source_id": 2, "target_id": 1, "resolution_method": "type_flow",
                "confidence": 0.95, "metadata": "receiver_type=Repo;dataflow=save"}],
        edge_metadata=[(10, "receiver_type", "Repo"), (10, "dataflow", "save")],
    )
    con = gw._connect_ro(path)
    try:
        callers, receivers = gw._fact_callers(con, [1])
    finally:
        con.close()
    assert receivers == ["Repo"]
    assert any(c["name"] == "handler" for c in callers)


def test_b24_fact_callers_fallback_parses_raw_metadata(tmp_path):
    """Old graph.db (no edge_metadata table) -> receiver_type parsed from edges.metadata."""
    path = _mk_db(
        tmp_path,
        nodes=[{"id": 1, "name": "save", "file_path": "db.py", "start_line": 1},
               {"id": 2, "name": "handler", "file_path": "h.py", "start_line": 1}],
        edges=[{"id": 10, "source_id": 2, "target_id": 1, "resolution_method": "type_flow",
                "confidence": 0.95, "metadata": "receiver_type=Repo;dataflow=save"}],
        # no edge_metadata table
    )
    con = gw._connect_ro(path)
    try:
        _callers, receivers = gw._fact_callers(con, [1])
    finally:
        con.close()
    assert receivers == ["Repo"]
