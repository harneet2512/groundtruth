"""T0->T2 localization re-slot — the OFFLINE reactive mechanism (2026-07-12).

GT's ranked localization ANSWER is re-homed from the T0 baked ``<gt-localization>`` brief
tag to the D2/post-search reactive producer ``gateway._produce_ranked_localization``, so it
rides the agent's OWN grep channel as ``path:line:sym`` rows (hedge-free) where the agent can
act on it. This suite pins the OFFLINE mechanism — deterministic fixtures, no rebake, no spend:

  * the reactive producer (issue-keyed; fires even on a behavior-PHRASE grep — stratum B),
  * the ``ranked-list`` native renderer + adapter dispatch (grep-native, hedge-free, leak=0),
  * the arbitration rank (a ranked FILE answer wins the FIRST search dose over def_partition),
  * the registry ``deliver_by`` re-slot (task_start -> search_result: a reactive localization
    envelope DELIVERs at search under enforcement instead of EXPIRED_LATE-dropping),
  * byte-identical-off (GT_LOC_RESLOT unset -> no localization envelope is EVER produced).

TTD receipts (captured this session):
  * REGISTRY: ``test_registry_localization_timing_reslot`` was RED before the C1
    ``deliver_by`` change (task_start -> search(1) > want(0) -> EXPIRED_LATE); GREEN after.
  * PRODUCER WIRING: ``test_augment_delivers_ranked_localization_native_rows`` was RED before
    the ``augment`` search-dispatch line (augment produced 0 localization envelopes); GREEN after.
Each guard additionally carries an in-suite monkeypatch MUTATION that reddens it.

localize() is monkeypatched to a controlled result in the unit tests (the producer's job is
resolve-lines -> leak-clean envelope -> arbitrate -> route; localize()'s RANKING is untouched
and separately owned). One integration test drives REAL localize() on a fixture graph.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

import groundtruth.runtime.gateway as gw
import groundtruth.runtime.adapters.miniswe as ad
import groundtruth.runtime.native_render as nr
from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _mk_graph(tmp_path, nodes, edges=(), meta=None, name="graph.db"):
    db = str(tmp_path / name)
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
        "CREATE TABLE project_meta(key TEXT PRIMARY KEY, value TEXT);"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("is_test", 0), "python"))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence)"
            " VALUES(?,?,?,?,?,?)",
            (e["id"], e["source_id"], e["target_id"], "CALLS",
             e.get("resolution_method", "import"), e.get("confidence", 1.0)))
    for k, v in (meta or {}).items():
        con.execute("INSERT INTO project_meta(key,value) VALUES(?,?)", (k, v))
    con.commit()
    con.close()
    return db


_SEQ = [0]


def _reslot_graph(tmp_path):
    """auth.py (gold, anchor verify_token@10) + token.py (refresh@20) + a TEST file that must
    be firewalled out. Unique db name per call (RO connections may hold the file)."""
    _SEQ[0] += 1
    return _mk_graph(tmp_path, [
        {"id": 1, "name": "verify_token", "file_path": "auth.py", "start_line": 10},
        {"id": 2, "name": "helper", "file_path": "auth.py", "start_line": 40},
        {"id": 3, "name": "refresh", "file_path": "token.py", "start_line": 20},
        {"id": 4, "name": "test_login", "file_path": "tests/test_auth.py",
         "start_line": 3, "is_test": 1},
    ], edges=[{"id": 1, "source_id": 3, "target_id": 1}], name=f"g{_SEQ[0]}.db")


def _fake_localize(*cands, anchors=("verify_token",)):
    """A deterministic localize() stand-in: a result object exposing .candidates
    (each with .file_path) and .anchor_symbols — the only surface the producer reads."""
    def _fn(issue_text, graph_db, *, repo_root="", **kw):
        return types.SimpleNamespace(
            candidates=[types.SimpleNamespace(file_path=f) for f in cands],
            anchor_symbols=list(anchors),
        )
    return _fn


def _search_ev(command='grep -rn "verify token" .', output=""):
    # a behavior-PHRASE operand -> search_pattern() is None (stratum-B blind spot) -> no
    # def_partition; proves the ranked-localization producer fires issue-keyed regardless.
    return gw.ToolEvent(kind="search", command=command, output=output, action_index=0)


def _state(tmp_path, db, issue="verify token verification fails"):
    return gw.GatewayState(graph_db=db, repo_root=str(tmp_path), issue_text=issue)


def _gateway_on(monkeypatch):
    """augment early-returns [] unless the master flag GT_GATEWAY is on (gateway.py:2023)."""
    monkeypatch.setenv("GT_GATEWAY", "1")


# =========================================================================== #
# 1. RENDERER — grep-native rows, hedge-free, identity-firewalled (leak=0).
# =========================================================================== #
def test_render_ranked_list_is_grep_native_and_hedge_free():
    out = nr.render_ranked_list_native(
        [("auth.py", 10, "verify_token"), ("token.py", 20, "refresh")])
    assert out == "auth.py:10:verify_token\ntoken.py:20:refresh"
    # grep-native: NO GT tag, NO "confirm with grep" hedge.
    assert "<gt-" not in out.lower()
    assert "confirm" not in out.lower() and "grep" not in out.lower()
    assert nr.contains_test_identity(out) is False


def test_render_ranked_list_drops_test_row_and_mutation_reddens(monkeypatch):
    row = [("tests/test_helpers.py", 3, "test_foo")]
    # REAL firewall: a test-file candidate is DROPPED (correct-or-quiet -> "").
    assert nr.render_ranked_list_native(row) == ""
    # MUTATION: neuter the per-row test-path DROP -> the row survives to output (non-empty)
    # instead of being dropped. Proves the inherited render_def_rows_native `_is_test_path`
    # drop is load-bearing for the ranked-list path. (`_final_scrub` remains a SECOND firewall
    # layer — hence the surviving row is token-scrubbed, not the raw path — defense in depth.)
    monkeypatch.setattr(nr, "_is_test_path", lambda *a, **k: False)
    assert nr.render_ranked_list_native(row) != ""


# =========================================================================== #
# 2. PRODUCER + AUGMENT — the reactive ranked-localization at D2 (RED before the dispatch).
# =========================================================================== #
def test_augment_delivers_ranked_localization_native_rows(tmp_path, monkeypatch):
    """RED before the augment search-dispatch (augment produced 0 localization envelopes).
    GREEN: exactly ONE localization envelope, top-N ranked rows, TEST candidate firewalled,
    rendered grep-native + hedge-free + leak=0."""
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    monkeypatch.setattr(gw, "_localize",
                        _fake_localize("auth.py", "token.py", "tests/test_auth.py"))
    db = _reslot_graph(tmp_path)
    envs = gw.augment(_search_ev(), _state(tmp_path, db))
    locs = [e for e in envs if e.evidence_type == "localization"]
    assert len(locs) == 1
    env = locs[0]
    assert env.producer == "ranked_localization"
    assert env.target == "auth.py" and env.fact_id == "verify_token"
    # the TEST candidate (tests/test_auth.py) is firewalled out of provenance.
    assert env.provenance == (("auth.py", 10), ("token.py", 20))
    assert env.native_args and env.native_args["rows"] == [
        ("auth.py", 10, "verify_token"), ("token.py", 20, "refresh")]
    # rendered NATIVE (the RL-native D2 form): grep rows, no tag, no hedge, no test identity.
    rendered = ad.render_envelope(env, native=True)
    assert "auth.py:10:verify_token" in rendered and "token.py:20:refresh" in rendered
    assert "<gt-" not in rendered.lower()
    assert "confirm" not in rendered.lower()
    assert "test_auth" not in rendered
    assert nr.contains_test_identity(rendered) is False


def test_ranked_localization_wins_dose_over_def_partition(tmp_path, monkeypatch):
    """A bare-symbol grep that ALSO fires def_ref_partition: the ranked FILE answer out-doses
    the symbol def-partition (rank 37 > 35) on the FIRST search turn."""
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    monkeypatch.setattr(gw, "_localize", _fake_localize("auth.py", "token.py"))
    # foo defined in two files -> AMBIGUOUS_HIT -> def_ref_partition also produced.
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "verify_token", "file_path": "auth.py", "start_line": 10},
        {"id": 2, "name": "refresh", "file_path": "token.py", "start_line": 20},
        {"id": 3, "name": "foo", "file_path": "auth.py", "start_line": 50},
        {"id": 4, "name": "foo", "file_path": "token.py", "start_line": 60},
    ], edges=[{"id": 1, "source_id": 1, "target_id": 3}], name=f"g{_SEQ[0] + 100}.db")
    ev = gw.ToolEvent(kind="search", command="grep -rn foo .",
                      output="auth.py:50:def foo\ntoken.py:60:def foo", action_index=0)
    winner = ad.arbitrate(gw.augment(ev, _state(tmp_path, db)))
    assert winner is not None and winner.evidence_type == "localization"


def test_localize_memoized_once_per_episode(tmp_path, monkeypatch):
    calls = {"n": 0}

    def _counting(*a, **k):
        calls["n"] += 1
        return types.SimpleNamespace(
            candidates=[types.SimpleNamespace(file_path="auth.py")],
            anchor_symbols=["verify_token"])

    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    monkeypatch.setattr(gw, "_localize", _counting)
    db = _reslot_graph(tmp_path)
    st = _state(tmp_path, db)
    gw._ranked_localization_rows(st)
    gw._ranked_localization_rows(st)
    gw._ranked_localization_rows(st)
    assert calls["n"] == 1  # localize() ran exactly once (episode-memoized)


# =========================================================================== #
# 3. LEAK — a test-shaped TOP candidate is dropped by the producer firewall.
# =========================================================================== #
def test_producer_drops_leaky_top_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    # the localizer's #1 is a test path -> the producer firewall drops it; auth.py survives.
    monkeypatch.setattr(gw, "_localize",
                        _fake_localize("tests/test_auth.py", "auth.py"))
    db = _reslot_graph(tmp_path)
    envs = gw.augment(_search_ev(), _state(tmp_path, db))
    env = next(e for e in envs if e.evidence_type == "localization")
    assert env.target == "auth.py"  # NOT the test path
    assert all("test" not in f for f, _ in env.provenance)
    assert nr.contains_test_identity(ad.render_envelope(env, native=True)) is False


# =========================================================================== #
# 4. REGISTRY TIMING (C1) — reactive localization DELIVERs at search under enforcement.
# =========================================================================== #
def _loc_env(preferred_event: str) -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="ranked_localization", fact_id="verify_token", target="auth.py",
        evidence_type="localization", payload=("auth.py:10:verify_token",),
        provenance=(("auth.py", 10),), confidence=0.5, tier="HYPOTHESIS",
        graph_revision="g", valid_until="", preferred_event=preferred_event)


def test_registry_localization_timing_reslot(monkeypatch):
    """RED before C1 (deliver_by=task_start): a search-fired localization routes EXPIRED_LATE
    (cur=search(1) > want=task_start(0)). GREEN after C1 (deliver_by=search_result): DELIVER."""
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    st = gw.GatewayState()  # no graph_db -> freshness never stales
    # the honest declared boundary is now search_result.
    assert fr.required_event("localization") == "search_result"
    # search -> DELIVER (cur 1 == want 1)
    assert gw.route_delivery(_loc_env("search"), gw.ToolEvent(kind="search"), st) == gw.ROUTE_DELIVER
    # edit / submit -> EXPIRED_LATE (cur 3/5 > want 1): a reactive answer offered too late.
    assert gw.route_delivery(_loc_env("edit"), gw.ToolEvent(kind="edit"), st) == gw.ROUTE_EXPIRED_LATE
    assert gw.route_delivery(_loc_env("submit"), gw.ToolEvent(kind="submit"), st) == gw.ROUTE_EXPIRED_LATE
    # step0 (an "other"/pre-search event, ordinal 0) -> DEFER (cur 0 < want 1): re-offered later.
    assert gw.route_delivery(_loc_env("other"), gw.ToolEvent(kind="other"), st) == gw.ROUTE_DEFER


def test_registry_timing_mutation_reverts_to_expired(monkeypatch):
    """MUTATION of C1: revert the declared boundary to task_start -> a search-fired reactive
    localization EXPIRES (the self-cert the re-slot fixed). Proves C1 is load-bearing."""
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    monkeypatch.setattr(gw, "_fr_required_event",
                        lambda et: "task_start" if et == "localization" else fr.required_event(et))
    st = gw.GatewayState()
    assert gw.route_delivery(_loc_env("search"), gw.ToolEvent(kind="search"), st) == gw.ROUTE_EXPIRED_LATE


def test_trace_frame_localization_alias_unaffected_by_c1():
    """trace_frame aliases to the localization DECISION but overrides deliver_by to failure_obs
    and is reactive — C1 (localization base -> search_result) must NOT disturb it."""
    assert fr.required_event("trace_frame") == "failure_obs"
    assert fr.is_reactive("trace_frame") is True
    assert fr.required_renderer("trace_frame") == "ranked-list"


# =========================================================================== #
# 5. BYTE-IDENTICAL-OFF + adapter-dispatch load-bearing.
# =========================================================================== #
def test_byte_identical_off_no_localization_envelope(tmp_path, monkeypatch):
    _gateway_on(monkeypatch)
    monkeypatch.setattr(gw, "_localize", _fake_localize("auth.py", "token.py"))
    db = _reslot_graph(tmp_path)
    ev = _search_ev()
    # flag OFF -> augment produces NO localization envelope (byte-identical: nothing added).
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    off = gw.augment(ev, _state(tmp_path, db))
    assert not any(e.evidence_type == "localization" for e in off)
    # flag ON -> exactly one appears (the only delta).
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    on = gw.augment(ev, _state(tmp_path, db))
    assert sum(1 for e in on if e.evidence_type == "localization") == 1


def test_adapter_dispatch_is_load_bearing(tmp_path, monkeypatch):
    """MUTATION: remove the ``localization`` native dispatch entry -> the envelope falls back to
    _render_generic (tag-wrapped), NOT the grep-native rows. Proves the dispatch entry matters."""
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    monkeypatch.setattr(gw, "_localize", _fake_localize("auth.py", "token.py"))
    db = _reslot_graph(tmp_path)
    env = next(e for e in gw.augment(_search_ev(), _state(tmp_path, db))
               if e.evidence_type == "localization")
    # real: native dispatch -> grep rows, tag-free.
    assert "<gt-" not in ad.render_envelope(env, native=True).lower()
    mutant = dict(ad._NATIVE_CLASS_RENDERERS)
    mutant.pop("localization")
    monkeypatch.setattr(ad, "_NATIVE_CLASS_RENDERERS", mutant)
    # mutant: no native mapping -> _render_generic world-fact voice (still tag-free under
    # native, but NOT via the ranked-list renderer). The dispatch removal changed the path.
    assert ad._render_native_class(env) == ""


# =========================================================================== #
# 6. INTEGRATION — REAL localize() on a fixture graph drives the producer end-to-end.
# =========================================================================== #
def test_real_localize_integration(tmp_path, monkeypatch):
    """Drive the REAL localize() (not a stand-in) through augment. Deterministic over the fixed
    graph. Tolerant of an empty candidate set in a bare env (localize is separately owned): the
    contract asserted here is producer-integration + leak=0, never a ranking number."""
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    _gateway_on(monkeypatch)
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "verify_token", "file_path": "auth.py", "start_line": 10},
        {"id": 2, "name": "login", "file_path": "auth.py", "start_line": 30},
        {"id": 3, "name": "refresh", "file_path": "token.py", "start_line": 20},
    ], edges=[{"id": 1, "source_id": 3, "target_id": 1}])
    st = _state(tmp_path, db, issue="verify_token raises on an expired token")
    envs = gw.augment(_search_ev(), st)  # must not raise
    locs = [e for e in envs if e.evidence_type == "localization"]
    for env in locs:  # whatever the real ranking returns, it is leak-clean + real graph files
        assert env.target and "test" not in env.target
        assert nr.contains_test_identity(ad.render_envelope(env, native=True)) is False
