"""SM-0 Super-Mode spine — the EXECUTABLE fact-registry + registry-timing + freshness
single-source + Profile-2 preflight (2026-07-11).

The registry was DESCRIPTIVE: the kernel read only ``renderable`` (membership); every
registration's ``deliver_by`` / ``native_renderer`` / ``freshness_deps`` was STORED and never
consumed, timing was tautological (``route_delivery`` compared the self-stamped
``preferred_event`` — always the current event — to the current event), and freshness lived in
a parallel Gateway table that could silently diverge. This suite pins the fix, ALL gated behind
``GT_REGISTRY_ENFORCE`` (default OFF ⇒ byte-identical).

TTD: each block names the RED it was written against + the ≥2 biting mutations. The suite runs
GREEN with the flag OFF and with ``GT_REGISTRY_ENFORCE=1`` (proven in the reconciliation table
below — every current producer passes enforcement).
"""

from __future__ import annotations

import sqlite3

import pytest

import groundtruth.runtime.gateway as gw
from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime import rl_profile as rp
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope


# =========================================================================== #
# 1. fact_registry — the registry is EXECUTABLE (accessors resolve the shipped vocabulary).
# =========================================================================== #
def test_registration_for_resolves_direct_alias_and_base():
    # RED before registration_for existed. Resolves direct key, an alias, and a base:suffix.
    assert fr.registration_for("def_partition").fact_class == "def_partition"
    assert fr.registration_for("def_ref_partition").fact_class == "def_partition"   # alias
    assert fr.registration_for("missing_role:handler").fact_class == "newfile_precedent"
    assert fr.registration_for("totally_bogus") is None


def test_required_event_uses_declared_deliverby_and_trace_override():
    # A fixed-lifecycle class reports its canonical deliver_by.
    assert fr.required_event("def_ref_partition") == "search_result"
    assert fr.required_event("signature_mismatch") == "edit_result"
    assert fr.required_event("covering_verdict") == "test_result"
    assert fr.required_event("new_file_destination") == "failed_search"
    # MUTATION-1 (bites here): delete the trace_frame entry from _EVIDENCE_TYPE_DELIVER_BY ->
    # trace_frame inherits localization's task_start and this assert flips to "task_start".
    assert fr.required_event("trace_frame") == "failure_obs"
    assert fr.required_event("bogus") is None


def test_required_renderer_binds_class_to_native_form():
    assert fr.required_renderer("def_ref_partition") == "grep-native"
    assert fr.required_renderer("trace_frame") == "ranked-list"
    assert fr.required_renderer("covering_verdict") == "test-native"
    assert fr.required_renderer("bogus") is None
    # every registered class HAS a renderer (import-time _self_check guarantees it).
    for fc in fr.all_fact_classes():
        assert fr.required_renderer(fc)


def test_trace_frame_is_observation_reactive():
    # RED before is_reactive existed. trace_frame is reactive (no fixed boundary); the
    # fixed-lifecycle classes are NOT. MUTATION-2 (bites here): empty _REACTIVE_EVIDENCE_TYPES
    # -> trace_frame becomes fixed-boundary -> a trace on a non-test obs EXPIRES (see the
    # gateway reactive test below).
    assert fr.is_reactive("trace_frame") is True
    for fc in ("def_ref_partition", "signature_mismatch", "covering_verdict", "cochange_partner"):
        assert fr.is_reactive(fc) is False


# =========================================================================== #
# 2. fact_registry — freshness is the SINGLE SOURCE, tied back to the §1 deps.
# =========================================================================== #
def test_freshness_surfaces_are_single_source_and_per_type():
    # per-evidence-type granularity is preserved (body_concept != its def_partition siblings).
    assert fr.freshness_surfaces("def_ref_partition") == ("nodes", "edges")
    assert fr.freshness_surfaces("body_concept") == ("nodes", "edges", "content_fts")
    assert fr.freshness_surfaces("cochange_partner") == ("nodes", "edges", "cochanges")
    assert fr.freshness_surfaces("missing_role:handler") == ("nodes", "edges", "closure")
    assert fr.freshness_surfaces("bogus") is None
    assert fr.is_patch_bound("covering_verdict") is True
    assert fr.is_patch_bound("def_ref_partition") is False


def test_gateway_reads_freshness_from_registry_single_source():
    # the Gateway's alias IS the registry object (no divergent copy).
    assert gw._FACTCLASS_FRESHNESS_SURFACES == dict(fr.FRESHNESS_SURFACES)
    assert gw._PATCH_BOUND_FACTCLASSES == fr.PATCH_BOUND_FACTCLASSES


def test_freshness_crosscheck_ties_patchbound_and_surfaces_to_registry():
    # patch-boundness is DERIVED from the §1 dep (covering_red declares patch_rev). MUTATION-3
    # (bites _self_check_executable at import): drop patch_rev from covering_red.freshness_deps.
    for pb in fr.PATCH_BOUND_FACTCLASSES:
        assert "patch_rev" in fr.registration_for(pb).freshness_deps
    # every operational surface key resolves to a registered class.
    for et in fr.FRESHNESS_SURFACES:
        assert fr.registration_for(et) is not None


# =========================================================================== #
# 3. gateway — route_delivery timing derives from the REGISTRY under enforcement.
# =========================================================================== #
def _env(evidence_type: str, preferred_event: str, *, valid_until: str = "") -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="p", fact_id="x", target="a/x.py", evidence_type=evidence_type,
        payload=("def: a/x.py:1",), provenance=(("a/x.py", 1),), confidence=0.9,
        tier="VERIFIED", graph_revision="g", valid_until=valid_until,
        preferred_event=preferred_event,
    )


def test_route_registry_want_defers_wrong_event_under_enforcement(monkeypatch):
    """A covering_verdict (DECLARED boundary = test) offered on a SEARCH event: DEFER under
    enforcement (too early), DELIVER when off (self-stamped preferred_event=search)."""
    st = gw.GatewayState()  # no graph_db -> no freshness stale
    ev = gw.ToolEvent(kind="search")
    env = _env("covering_verdict", preferred_event="search")  # self-stamp says on-time
    # OFF: want = self-stamp (search) == cur -> DELIVER
    monkeypatch.delenv("GT_REGISTRY_ENFORCE", raising=False)
    assert gw.route_delivery(env, ev, st) == gw.ROUTE_DELIVER
    # ON: want = registry deliver_by (test=4) > cur (search=1) -> DEFER
    # MUTATION-4 (bites): revert route_delivery to use _event_ordinal(env.preferred_event)
    # under the flag -> this DELIVERs and the assert fails.
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    assert gw.route_delivery(env, ev, st) == gw.ROUTE_DEFER


def test_route_registry_want_expires_late_event_under_enforcement(monkeypatch):
    st = gw.GatewayState()
    ev = gw.ToolEvent(kind="submit")  # cur = 5
    env = _env("def_ref_partition", preferred_event="submit")  # boundary = search (1)
    monkeypatch.delenv("GT_REGISTRY_ENFORCE", raising=False)
    assert gw.route_delivery(env, ev, st) == gw.ROUTE_DELIVER  # off: self-stamp submit==cur
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    assert gw.route_delivery(env, ev, st) == gw.ROUTE_EXPIRED_LATE  # on: 5 > 1


def test_reactive_trace_frame_is_on_time_at_any_observation(monkeypatch):
    """A reactive trace_frame is on-time at WHATEVER observation carries it (test/view/edit/
    other), while a fixed-lifecycle def_ref_partition on those same non-search events EXPIRES.
    MUTATION-2 restated: empty _REACTIVE_EVIDENCE_TYPES -> trace_frame pins to failure_obs(4)
    and a trace on a view/other event DEFER/EXPIREs -> this bites."""
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    st = gw.GatewayState()
    for kind in ("test", "view", "edit", "other", "submit"):
        env = _env("trace_frame", preferred_event="test")
        assert gw.route_delivery(env, gw.ToolEvent(kind=kind), st) == gw.ROUTE_DELIVER, kind
    # contrast: a FIXED-boundary class is NOT reactive -> wrong-event routing bites.
    env_fixed = _env("def_ref_partition", preferred_event="search")
    assert gw.route_delivery(env_fixed, gw.ToolEvent(kind="view"), st) == gw.ROUTE_EXPIRED_LATE


# --------------------------------------------------------------------------- #
# THE RECONCILIATION TABLE, as an executable pin: every CURRENT gateway producer's firing
# event vs its registry deliver_by -> DELIVER under enforcement (nothing valid is rejected).
# --------------------------------------------------------------------------- #
_RECONCILIATION = [
    # (firing kind, evidence_type, registry deliver_by fine event)
    ("search", "def_ref_partition",     "search_result"),
    ("search", "wrong_surface",         "search_result"),
    ("search", "name_fold",             "search_result"),
    ("search", "body_concept",          "search_result"),
    ("search", "new_file_destination",  "failed_search"),
    ("search", "missing_role:handler",  "failed_search"),
    ("edit",   "signature_mismatch",    "edit_result"),
    ("edit",   "companion_surface",     "edit_result"),
    ("edit",   "cochange_partner",      "first_view_edit"),
    ("test",   "covering_verdict",      "test_result"),
    ("test",   "trace_frame",           "failure_obs"),   # reactive
]


@pytest.mark.parametrize("kind,etype,fine_event", _RECONCILIATION)
def test_reconciliation_every_producer_passes_enforcement(monkeypatch, kind, etype, fine_event):
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    assert fr.required_event(etype) == fine_event
    st = gw.GatewayState()
    env = _env(etype, preferred_event=kind)
    assert gw.route_delivery(env, gw.ToolEvent(kind=kind), st) == gw.ROUTE_DELIVER


# =========================================================================== #
# 4. gateway — the augment renderer-binding gate (registry ENFORCEMENT) + byte-identity OFF.
# =========================================================================== #
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


_DB_SEQ = [0]


def _ambiguous_search_state(tmp_path):
    # foo defined in two files -> AMBIGUOUS_HIT -> _produce_def_ref_partition fires. Unique db
    # name per call (read-only connections may hold the file across calls in one test).
    _DB_SEQ[0] += 1
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "foo", "file_path": "a.py", "start_line": 1},
        {"id": 2, "name": "bar", "file_path": "b.py", "start_line": 1},
        {"id": 3, "name": "foo", "file_path": "c.py", "start_line": 1},
    ], edges=[{"id": 1, "source_id": 2, "target_id": 1}], name=f"g{_DB_SEQ[0]}.db")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    ev = gw.ToolEvent(kind="search", command="grep -rn foo .",
                      output="a.py:1:def foo\nc.py:1:def foo", action_index=0)
    return st, ev


def test_renderer_gate_drops_rendererless_class_under_enforcement(tmp_path, monkeypatch):
    """A class with NO available renderer is dropped under enforcement (correct-or-quiet).
    Simulated by nulling the renderer for the emitted class. MUTATION-5 (bites): remove the
    ``_registry_enforce() and not _fr_required_renderer(...)`` gate -> the fact ships despite
    a null renderer and augment returns non-[]."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    st, ev = _ambiguous_search_state(tmp_path)
    # baseline: enforcement ON, renderer present -> delivers.
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    assert gw.augment(ev, st)
    # now null the renderer the gateway resolves -> the gate drops it.
    st2, ev2 = _ambiguous_search_state(tmp_path)
    monkeypatch.setattr(gw, "_fr_required_renderer", lambda et: None)
    assert gw.augment(ev2, st2) == []
    # flag OFF: the renderer gate is skipped -> delivers even with the null renderer.
    st3, ev3 = _ambiguous_search_state(tmp_path)
    monkeypatch.delenv("GT_REGISTRY_ENFORCE", raising=False)
    assert gw.augment(ev3, st3)


def test_enforcement_is_byte_identical_for_valid_producers(tmp_path, monkeypatch):
    """The delivered set is IDENTICAL with the flag OFF vs ON for a valid producer — enforcement
    rejects only genuinely mis-boundaried/renderer-less classes, never a valid fact."""
    monkeypatch.setenv("GT_GATEWAY", "1")

    def _run():
        st, ev = _ambiguous_search_state(tmp_path)
        return [(e.producer, e.evidence_type, e.target) for e in gw.augment(ev, st)]

    monkeypatch.delenv("GT_REGISTRY_ENFORCE", raising=False)
    off = _run()
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    on = _run()
    assert off and off == on


def test_trace_frame_augment_delivers_on_test_under_enforcement(tmp_path, monkeypatch):
    """End-to-end: a failing-test observation with an in-repo traceback delivers trace_frame
    under enforcement (the reactive path keeps the genuinely-correct producer green)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    db = _mk_graph(tmp_path, [])
    out = (
        "Traceback (most recent call last):\n"
        '  File "svc/watch.py", line 12, in run\n'
        "    do()\n"
        "ValueError: boom\n"
    )
    ev = gw.ToolEvent(kind="test", command="pytest -q", output=out, action_index=1)
    envs = gw.augment(ev, gw.GatewayState(graph_db=db, repo_root=str(tmp_path)))
    assert any(e.evidence_type == "trace_frame" and "svc/watch.py" in e.target for e in envs)


# =========================================================================== #
# 5. rl_profile — Profile-2 members + manifest + preflight (SM-0).
# =========================================================================== #
def test_profile_2_is_profile_1_plus_super_mode_members():
    # SM-0 (GT_REGISTRY_ENFORCE) + SM-3 (the four activated-engine flags) + SM-4
    # (GT_EDIT_OVERLAY, re-lit for the episode-overlay stale-drop) + SM-5 (the global
    # arbiter + the two Gateway producers its pool needs) are the Super-Mode delta over
    # Profile-1. Dropping/adding one here makes this bite.
    assert rp.PROFILE_MEMBERS["2"] == rp.PROFILE_MEMBERS["1"] | {
        "GT_REGISTRY_ENFORCE",
        "GT_COMPLETION_CERT",
        "GT_HYPOTHESIS",
        "GT_VERIFICATION_PLAN",
        "GT_EDIT_OVERLAY",
        # SM-5 (2026-07-11): the single global dose + the two Gateway producers that
        # complete its pool on the mini run.
        "GT_GLOBAL_ARBITER",
        "GT_CHANGE_SURFACE",
        "GT_PATCH_DELTA",
        # SM-6 (2026-07-11): the step-0 baked-brief reduction (obligations + minimal
        # orientation; localization rides reactive def_partition). Baked-at-generation,
        # dormant until the SM-8 rebake.
        "GT_BRIEF_MINIMAL",
        # SM-9c (2026-07-11): the cross-session learned-delivery policy (per-repo causal-
        # consumption ledger; suppress a never-consumed class, rank-up a consumed one).
        "GT_XSESSION_MEMORY",
        # SM-9c rank-up (2026-07-12): the WINNER-PROMOTION half — a ladder-derived arbitration
        # boost stamped on a consumed class so it can WIN the adapter's <=1 dose (not merely sort).
        "GT_XSESSION_RANKUP",
        # SM-10 (2026-07-12): the two stratum-B BODY-content retrieval legs (the only
        # body-semantic surfaces): per-symbol body-BM25 (symbol_content_fts) + the semantic
        # leg's per-symbol body passages. Default-OFF localize() flag reads; Profile-2 lights them.
        "GT_CONTENT_LEG",
        "GT_SEM_BODY",
        # T0->T2 re-slot (2026-07-12): the reactive ranked-localization producer at the
        # D2/post-search boundary (gateway._produce_ranked_localization). Default-OFF; Profile-2
        # lights it so GT's ranked answer rides the agent's own grep channel post-search.
        "GT_LOC_RESLOT",
        # SM-3 D7 delivery (2026-07-12): model-facing CompletionCertificate delivery at the
        # submit turn (native pre-commit-hook failure block). Companion to GT_COMPLETION_CERT.
        "GT_CERT_DELIVERY",
        # #48 L6-FRESH (2026-07-12): the per-turn in-container reindex engine (edit-fresh
        # work-copy of the base graph.db). Runtime consumer gt_mini_patch._db_path; the
        # capability is the staged gt-index binary (substrate property, unmapped-by-convention).
        "GT_L6_FRESH",
        # SS-1..SS-N (2026-07-13): the SUPER-SEAM adherence sweep (29-task causal audit of run
        # 29236533134). GT_SS_ARBITER_V2 = arbiter defer-refire + repair_support relaxation +
        # empty-payload guard (global_arbiter/gateway); the 7 others are default-OFF seam-code
        # properties (novelty/dedup2/coherence/recovery/provenance/late-drop/ack-metrics), all
        # unmapped-by-convention. Dropping/adding one here makes this bite.
        "GT_SS_NOVELTY",
        "GT_SS_DEDUP2",
        "GT_SS_COHERENCE_V2",
        "GT_SS_RECOVERY_V2",
        "GT_SS_PROVENANCE",
        "GT_SS_LATE_DROP",
        "GT_SS_ACK_METRICS",
        "GT_SS_ARBITER_V2",
    }
    # SM-4: GT_EDIT_OVERLAY is now profile-activated (model-facing stale-drop) -> PRESENT.
    assert "GT_EDIT_OVERLAY" in rp.PROFILE_MEMBERS["2"]
    got = rp.resolve_profile({"GT_RL_PROFILE": "2"})
    assert got.get("GT_REGISTRY_ENFORCE") == "1"
    # every PROFILE-ACTIVATED SM-3/SM-4 engine flag fans out to "1" under Profile-2.
    for _f in ("GT_COMPLETION_CERT", "GT_HYPOTHESIS", "GT_VERIFICATION_PLAN", "GT_EDIT_OVERLAY"):
        assert got.get(_f) == "1", _f
    assert set(got) == rp.PROFILE_MEMBERS["2"]


def test_profile_2_manifest_renderers_match_registry():
    m = rp.PROFILE_MANIFESTS["2"]
    assert set(m.enabled_fact_classes) == set(fr.all_fact_classes())
    # MUTATION-6 (bites preflight below): corrupt any manifest renderer -> preflight aborts.
    for cls, renderer in m.renderer_per_class.items():
        assert fr.required_renderer(cls) == renderer


def test_preflight_2_clean_when_available():
    assert rp.preflight({"GT_RL_PROFILE": "2"}, set(rp.PROFILE_MEMBERS["2"])) == []


def test_preflight_2_renderer_drift_aborts(monkeypatch):
    # a renderer the registry cannot resolve (or that disagrees) is a fail-closed abort.
    monkeypatch.setattr(rp, "_registry_renderer", lambda cls: None)
    missing = rp.preflight({"GT_RL_PROFILE": "2"}, set(rp.PROFILE_MEMBERS["2"]))
    assert missing and all(x.startswith("renderer:") for x in missing)


def test_preflight_2_commit_pin(monkeypatch):
    pinned = rp.ProfileManifest(version="2", required_baked_commit="abc123",
                                renderer_per_class={}, retired_surfaces=frozenset())
    monkeypatch.setitem(rp.PROFILE_MANIFESTS, "2", pinned)
    avail = set(rp.PROFILE_MEMBERS["2"])
    # mismatch -> abort
    assert rp.preflight({"GT_RL_PROFILE": "2", "GT_BAKED_COMMIT": "WRONG"}, avail) == \
        ["GT_BAKED_COMMIT!=abc123"]
    # match -> clean
    assert rp.preflight({"GT_RL_PROFILE": "2", "GT_BAKED_COMMIT": "abc123"}, avail) == []
    # empty pin -> skipped
    empty = rp.ProfileManifest(version="2", renderer_per_class={}, retired_surfaces=frozenset())
    monkeypatch.setitem(rp.PROFILE_MANIFESTS, "2", empty)
    assert rp.preflight({"GT_RL_PROFILE": "2"}, avail) == []


def test_preflight_2_forbidden_retired_surface(monkeypatch):
    # the forbidden-present mechanism: a retired surface flag that is ON aborts.
    manifest = rp.ProfileManifest(version="2", renderer_per_class={},
                                  retired_surfaces=frozenset({"GT_OLD_PULLTOOLS"}))
    monkeypatch.setitem(rp.PROFILE_MANIFESTS, "2", manifest)
    avail = set(rp.PROFILE_MEMBERS["2"])
    assert rp.preflight({"GT_RL_PROFILE": "2", "GT_OLD_PULLTOOLS": "1"}, avail) == \
        ["retired:GT_OLD_PULLTOOLS"]
    assert rp.preflight({"GT_RL_PROFILE": "2"}, avail) == []  # absent -> clean


def test_preflight_1_unchanged_no_manifest():
    # Profile 1 has no manifest -> the member-missing semantics are exactly as before.
    avail = set(rp.PROFILE_MEMBERS["1"]) - {"GT_STEER_NATIVE"}
    assert rp.preflight({"GT_RL_PROFILE": "1"}, avail) == ["GT_STEER_NATIVE"]


def test_main_profile_2_emits_registry_enforce_export():
    class _Buf:
        def __init__(self):
            self.s = ""

        def write(self, x):
            self.s += x
            return len(x)

    out, err = _Buf(), _Buf()
    avail = ",".join(sorted(rp.PROFILE_MEMBERS["2"]))
    rc = rp.main(["--emit-exports"],
                 env={"GT_RL_PROFILE": "2", "GT_RL_PROFILE_AVAILABLE": avail},
                 out=out, err=err)
    assert rc == 0
    assert "export GT_REGISTRY_ENFORCE=1" in out.s
