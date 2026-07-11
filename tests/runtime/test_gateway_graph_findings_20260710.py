"""Gateway graph→consumer findings (Fable bounce 2026-07-10).

Graph-F8 (count EDGES not callers): ``_fact_callers`` has no DISTINCT, so one caller with
two call sites rendered "fact-tier callers: 2". The rendered line must count DISTINCT
(name,file) callers — parity with ``_test_ref_count``'s ``COUNT(DISTINCT source_id)``.

Graph-F3 (freshness keyed per fact CLASS, not per producer): ``patch_delta`` emits
``cochange_partner`` (dep: the ``cochanges`` surface) but freshness keyed on the producer
= (nodes,edges), so a cochange-surface change shipped a stale cochange fact as fresh. And
``covering_verdict`` fell to the whole ``graph_rev`` while its dep is the patch, so a graph
change could falsely stale a patch-bound verdict.

Graph-F2 (renderable is real + vocabulary unified): every ``evidence_type`` the gateway
actually emits must be ``renderable()==True`` after unifying the registry vocabulary, and
the augment render path must gate on it (an unregistered fact class never renders).
"""
from __future__ import annotations

import sqlite3

from groundtruth.runtime.fact_registry import renderable
from groundtruth.runtime.gateway import (
    KIND_EDIT,
    KIND_SEARCH,
    KIND_TEST,
    ROUTE_DELIVER,
    ROUTE_STALE,
    CoveringResult,
    GatewayState,
    ToolEvent,
    _def_partition_body,
    _mk_add,
    _resolve_symbol_defs,
    _revisions_for,
    route_delivery,
)
from groundtruth.runtime.covering_runner import _connect_ro


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------
def _mk_graph(tmp_path, nodes, edges, *, meta=None):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
        "CREATE TABLE project_meta(key TEXT PRIMARY KEY, value TEXT);"
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
    for k, v in (meta or {}).items():
        con.execute("INSERT INTO project_meta(key,value) VALUES(?,?)", (k, v))
    con.commit()
    con.close()
    return db


def _set_meta(db, key, value):
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO project_meta(key,value) VALUES(?,?)", (key, value))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Graph-F8 — fact-tier callers counts DISTINCT callers, not call sites.
# ---------------------------------------------------------------------------
def test_fact_tier_callers_counts_distinct_callers(tmp_path):
    db = _mk_graph(
        tmp_path,
        nodes=[
            {"id": 1, "name": "foo", "file_path": "a.py", "label": "Function", "start_line": 1},
            {"id": 2, "name": "bar", "file_path": "b.py", "label": "Function", "start_line": 1},
        ],
        # ONE caller (bar), TWO call sites (two CALLS edges from source_id=2 to foo).
        edges=[
            {"id": 1, "source_id": 2, "target_id": 1, "source_line": 10, "resolution_method": "import"},
            {"id": 2, "source_id": 2, "target_id": 1, "source_line": 20, "resolution_method": "import"},
        ],
    )
    con = _connect_ro(db)
    try:
        info = _resolve_symbol_defs(con, "foo", str(tmp_path))
    finally:
        con.close()
    assert info is not None
    body = _def_partition_body(info)
    assert "fact-tier callers: 1" in body, body
    assert "fact-tier callers: 2" not in body


# ---------------------------------------------------------------------------
# Graph-F3 — per-fact-CLASS freshness.
# ---------------------------------------------------------------------------
def _meta_state(tmp_path):
    db = _mk_graph(
        tmp_path,
        nodes=[{"id": 1, "name": "foo", "file_path": "a.py", "label": "Function", "start_line": 1}],
        edges=[],
        meta={
            "post_revision": "post0",
            "subrev_nodes": "n0",
            "subrev_edges": "e0",
            "subrev_cochanges": "c0",
            "subrev_content_fts": "f0",
            "subrev_closure": "cl0",
        },
    )
    return db, GatewayState(graph_db=db, repo_root=str(tmp_path))


def _edit_ev():
    return ToolEvent(kind=KIND_EDIT, command="edit a.py")


def test_cochange_fact_stales_on_cochanges_surface_change(tmp_path):
    db, state = _meta_state(tmp_path)
    ev = _edit_ev()
    env = _mk_add(state, ev, fact_kind="cochange_partner", target="a.py",
                  body_lines=["co-changed with the edit in 3 commits"], evidence=[],
                  tier="INFO", producer="patch_delta", symbol="a.py")
    # Fresh at build -> delivers.
    assert route_delivery(env, ev, state) == ROUTE_DELIVER
    # A change to the COCHANGES surface must stale a cochange fact (its declared dep).
    _set_meta(db, "subrev_cochanges", "c1")
    assert route_delivery(env, ev, state) == ROUTE_STALE


def test_cochange_fact_ignores_unrelated_surface_change(tmp_path):
    db, state = _meta_state(tmp_path)
    ev = _edit_ev()
    env = _mk_add(state, ev, fact_kind="cochange_partner", target="a.py",
                  body_lines=["co-changed with the edit in 3 commits"], evidence=[],
                  tier="INFO", producer="patch_delta", symbol="a.py")
    # content_fts is NOT a dep of cochange_partner -> its change must NOT stale it.
    _set_meta(db, "subrev_content_fts", "f1")
    assert route_delivery(env, ev, state) == ROUTE_DELIVER


def test_def_ref_partition_ignores_cochanges_change(tmp_path):
    db, state = _meta_state(tmp_path)
    ev = ToolEvent(kind=KIND_SEARCH, command="grep -rn foo .")
    env = _mk_add(state, ev, fact_kind="def_ref_partition", target="a.py",
                  body_lines=["def: a.py:1"], evidence=[("a.py", 1)],
                  tier="VERIFIED", producer="def_ref_partition", symbol="foo")
    # def_ref_partition depends only on nodes+edges; a cochanges change is irrelevant.
    _set_meta(db, "subrev_cochanges", "c9")
    assert route_delivery(env, ev, state) == ROUTE_DELIVER


def test_covering_verdict_is_patch_bound_not_graph_staled(tmp_path):
    db, state = _meta_state(tmp_path)
    ev = ToolEvent(kind=KIND_TEST, command="pytest")
    _gr, vu = _revisions_for(state, "covering_verdict")
    # Patch-bound: valid_until is NOT the graph composite (so a graph change can't
    # falsely stale a verdict computed against the working-tree patch).
    assert vu == "", f"covering_verdict must not be graph-keyed; got valid_until={vu!r}"
    env = _mk_add(state, ev, fact_kind="covering_verdict", target="a.py",
                  body_lines=["$ pytest", "[exit 1]"], evidence=[("a.py", 1)],
                  tier="WARNING", producer="covering", symbol="fail")
    # A graph surface change must NOT stale a patch-bound covering verdict.
    _set_meta(db, "subrev_nodes", "nX")
    _set_meta(db, "subrev_edges", "eX")
    assert route_delivery(env, ev, state) == ROUTE_DELIVER


# ---------------------------------------------------------------------------
# Graph-F2 — renderable() is real; the gateway vocabulary is registered.
# ---------------------------------------------------------------------------
# EVERY evidence_type the gateway emits (grep 'fact_kind=' in gateway.py) — the
# invariant is that each is renderable() after unifying the vocabulary.
_GATEWAY_EVIDENCE_TYPES = [
    "def_ref_partition",
    "wrong_surface",
    "name_fold",
    "body_concept",
    "new_file_destination",
    "missing_role:handler",  # dynamic suffix form
    "trace_frame",
    "signature_mismatch",
    "companion_surface",
    "cochange_partner",
    "covering_verdict",
]


def test_every_gateway_evidence_type_is_renderable():
    for et in _GATEWAY_EVIDENCE_TYPES:
        assert renderable(et), f"gateway emits {et!r} but renderable() blocks it (would silence a real fact)"


def test_unregistered_fact_class_is_not_renderable():
    assert not renderable("totally_made_up_fact")
    assert not renderable("")


# ---------------------------------------------------------------------------
# LEGACY-SCHEMA back-compat — an old graph.db with NO project_meta sub-revs must not
# crash and must fall back to graph_revision (freshness disabled, correct-or-quiet).
# ---------------------------------------------------------------------------
def test_cochange_fact_legacy_db_no_subrevs_delivers(tmp_path):
    # No project_meta table at all (pre-B-11 graph.db).
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,"
        " start_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, resolution_method TEXT, confidence REAL, metadata TEXT);"
    )
    con.commit()
    con.close()
    state = GatewayState(graph_db=db, repo_root=str(tmp_path))
    ev = _edit_ev()
    # _revisions_for must not crash; valid_until falls back to graph_revision (the legacy
    # file-hash, since post_revision is also absent) for a cochange fact.
    gr, vu = _revisions_for(state, "cochange_partner")
    assert vu == gr  # legacy invariant: valid_until == graph_revision when no sub-revs
    env = _mk_add(state, ev, fact_kind="cochange_partner", target="a.py",
                  body_lines=["co-changed with the edit in 3 commits"], evidence=[],
                  tier="INFO", producer="patch_delta", symbol="a.py")
    assert route_delivery(env, ev, state) == ROUTE_DELIVER  # no false stale on a legacy db


def test_augment_drops_unregistered_fact_class(tmp_path, monkeypatch):
    """The render GATE is REAL (not fiction): augment must DROP an addition whose
    evidence_type is unregistered. Monkeypatch a producer to emit a bogus fact class on a
    search event and assert augment ships nothing. (Break the ``if not _renderable`` gate
    in augment and this bites.)"""
    import os

    import groundtruth.runtime.gateway as gw

    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _mk_graph(
        tmp_path,
        nodes=[{"id": 1, "name": "foo", "file_path": "a.py", "label": "Function", "start_line": 1},
               {"id": 2, "name": "bar", "file_path": "b.py", "label": "Function", "start_line": 1},
               {"id": 3, "name": "foo", "file_path": "c.py", "label": "Function", "start_line": 1}],
        edges=[{"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import"}],
    )
    state = GatewayState(graph_db=db, repo_root=str(tmp_path))
    # foo spans a.py + c.py => AMBIGUOUS_HIT => _produce_def_ref_partition fires.
    ev = ToolEvent(kind=KIND_SEARCH, command="grep -rn foo .",
                   output="a.py:1:def foo\nc.py:1:def foo", action_index=0)

    real_mk_add = gw._mk_add

    def bogus_mk_add(state_, event_, **kw):
        kw["fact_kind"] = "an_unregistered_fact_class_xyz"  # not in REGISTRY or aliases
        return real_mk_add(state_, event_, **kw)

    monkeypatch.setattr(gw, "_mk_add", bogus_mk_add)
    out = gw.augment(ev, state)
    assert out == [], f"augment must drop an unregistered fact class, delivered {[e.evidence_type for e in out]}"
    assert os.environ.get("GT_GATEWAY") == "1"


def test_gateway_source_fact_kinds_all_renderable():
    """Regression guard: AST-scan gateway.py for every string-literal ``fact_kind=`` and
    assert each is renderable. A future producer that ships a new plain-literal fact
    class without registering it reddens here."""
    import ast

    import groundtruth.runtime.gateway as gw

    tree = ast.parse(open(gw.__file__, encoding="utf-8").read())
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "fact_kind" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    seen.add(kw.value.value)
    assert seen, "no literal fact_kind= found (scan broke)"
    for et in seen:
        assert renderable(et), f"gateway literal fact_kind={et!r} is not renderable()"
