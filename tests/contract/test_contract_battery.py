"""E-contract battery — the cross-module conformance LAWS, re-asserted as
single-purpose tests (P7-E consolidation).

This file does NOT duplicate the per-module suites (test_evidence_envelope.py,
test_verification_plan.py, ...). It imports the SHIPPED modules and re-asserts the
LAWS that span the enrichment stack, one law per test, each citing its home module.
A green run here is the mechanical proof that the whole contract still holds; the
per-module suites remain the exhaustive coverage.

STANDALONE: PYTHONIOENCODING=utf-8 python -m pytest tests/contract/ -q
"""
from __future__ import annotations

import dataclasses as dc

import pytest

from groundtruth.delivery.path_policy import is_deliverable
from groundtruth.runtime import episode_state as ES
from groundtruth.runtime import evidence_envelope as E
from groundtruth.runtime import gateway as GW
from groundtruth.runtime import hypothesis_ledger as HL
from groundtruth.runtime import submit_gate as SG
from groundtruth.runtime import verification_plan as VP

from tests.contract import mutation_registry


# --------------------------------------------------------------------------- #
# shared canonical envelope — a law-abiding VERIFIED fact (validate() == []).
# --------------------------------------------------------------------------- #
def _canonical_envelope() -> E.EvidenceEnvelope:
    return E.EvidenceEnvelope.build(
        producer="localization",
        fact_id="run",
        target="src/app.py",
        evidence_type="def_ref_partition",
        payload=("primary def: src/app.py:10",),
        provenance=(("src/app.py", 10),),
        confidence=0.9,
        tier=E.VERIFIED,
        graph_revision="rev1",
    )


def _rekey(env: E.EvidenceEnvelope, **overrides) -> E.EvidenceEnvelope:
    """dataclasses.replace with the dedup_key RE-DERIVED over the (possibly new)
    identity fields — so a violator test isolates exactly ONE law (a bare replace
    would ALSO trip the dedup-derivation law d, masking the law under test)."""
    merged = {**{f.name: getattr(env, f.name) for f in dc.fields(env)}, **overrides}
    merged["dedup_key"] = E.derive_dedup_key(
        merged["producer"], merged["evidence_type"], merged["target"],
        merged["fact_id"],
        E.content_signature(merged["payload"], merged["provenance"]),
    )
    return dc.replace(env, **{k: v for k, v in overrides.items()},
                      dedup_key=merged["dedup_key"])


# ===========================================================================
# EvidenceEnvelope.validate — laws a-g (home: runtime/evidence_envelope.py).
# One canonical valid envelope + one violator per law.
# ===========================================================================
def test_envelope_canonical_valid_is_clean():
    """A well-formed VERIFIED envelope passes validate() with ZERO violations
    (evidence_envelope.validate — the whole contract in the affirmative)."""
    assert E.validate(_canonical_envelope()) == []


def test_envelope_law_a_leak_target():
    """Law (a) LEAK: a test/demo/vendored target is a violation
    (evidence_envelope.validate + path_policy.is_deliverable chokepoint)."""
    v = _rekey(_canonical_envelope(), target="tests/test_app.py",
               provenance=(("tests/test_app.py", 10),),
               payload=("x",))
    viols = E.validate(v)
    assert any(x.startswith("leak:") for x in viols), viols


def test_envelope_law_b_tier_honesty():
    """Law (b) TIER HONESTY: VERIFIED requires nonempty provenance AND conf>=0.7
    (evidence_envelope.validate)."""
    v = E.EvidenceEnvelope.build(producer="p", fact_id="f", target="src/a.py",
                                 evidence_type="t", payload=("x",), provenance=(),
                                 confidence=0.9, tier=E.VERIFIED)
    assert any(x.startswith("tier:") for x in E.validate(v))


def test_envelope_law_c_unmeasured_advisory():
    """Law (c) UNMEASURED => ADVISORY: blocking_eligibility past advisory requires
    measured=True (evidence_envelope.validate)."""
    v = _rekey(_canonical_envelope(), blocking_eligibility=E.BLOCKING, measured=False)
    assert any(x.startswith("authority:") for x in E.validate(v))


def test_envelope_law_d_dedup_determinism():
    """Law (d) DEDUP DETERMINISM: dedup_key must equal the canonical 5-component
    derivation; a tampered key is a violation (evidence_envelope.validate)."""
    v = dc.replace(_canonical_envelope(), dedup_key="deadbeefdeadbeef")
    assert any(x.startswith("dedup_key:") for x in E.validate(v))


def test_envelope_law_e_receipt_needs_delivery_evidence():
    """Law (e) RECEIPT EVIDENCE: any receipt_state past 'none' requires a non-empty
    rendered_bytes_hash (evidence_envelope.validate — no receipt without delivery)."""
    v = _rekey(_canonical_envelope(), receipt_state=E.RECEIPT_ACTED,
               rendered_bytes_hash="")
    assert any(x.startswith("receipt_state:") for x in E.validate(v))


def test_envelope_law_f_hash_shape():
    """Law (f) HASH SHAPE: a hash field is empty or EXACTLY 64 lowercase hex
    (evidence_envelope.validate — the F1 boundary-shift width fix)."""
    v = _rekey(_canonical_envelope(), parent_observation_hash="xyz")
    assert any(x.startswith("parent_observation_hash:") for x in E.validate(v))


def test_envelope_law_g_delivery_suppression_exclusive():
    """Law (g) EXCLUSIVITY: delivery_reason and suppression_reason are mutually
    exclusive (evidence_envelope.validate)."""
    v = _rekey(_canonical_envelope(), delivery_reason="d", suppression_reason="s")
    assert any("mutually exclusive" in x for x in E.validate(v))


# ===========================================================================
# LEAK LAW — single chokepoint (home: delivery/path_policy.is_deliverable) +
# render carries no host metadata (home: evidence_envelope.render_bytes, law 9).
# ===========================================================================
@pytest.mark.parametrize("path,deliverable", [
    ("src/app.py", True),
    ("pkg/util.py", True),
    ("tests/test_app.py", False),      # test dir
    ("app/conftest.py", False),        # test basename
    ("vendor/lib.py", False),          # vendored
    ("node_modules/x/index.js", False),
    ("docs/example.py", False),        # demo/docs
])
def test_leak_chokepoint_is_deliverable(path, deliverable):
    """is_deliverable is THE single 'may this path reach the agent' predicate —
    rejecting test/demo/vendored, admitting genuine source (path_policy)."""
    assert is_deliverable(path) is deliverable


def test_render_bytes_carries_no_host_metadata():
    """LAW 9: render_bytes commits ONLY payload + provenance — host-side metadata
    (renderer_id, delivery_reason, episode_id, receipt_state) is NEVER rendered
    (evidence_envelope.render_bytes)."""
    env = E.EvidenceEnvelope.build(
        producer="p", fact_id="f", target="src/a.py", evidence_type="t",
        payload=("BODYLINE",), provenance=(("src/a.py", 3),),
        renderer_id="RENDERERTOKEN", delivery_reason="DELIVREASON",
        episode_id="EPISODETOKEN", receipt_state=E.RECEIPT_NONE)
    rb = E.render_bytes(env)
    assert b"BODYLINE" in rb and b"src/a.py" in rb          # content IS committed
    for token in (b"RENDERERTOKEN", b"DELIVREASON", b"EPISODETOKEN", b"receipt"):
        assert token not in rb, token                        # metadata is NOT


# ===========================================================================
# TITO locally-testable laws (home: runtime/evidence_envelope.py).
# ===========================================================================
def test_tito_law1_render_is_pure_append_only():
    """LAW 1 APPEND-ONLY: render_bytes is a pure read of a frozen dataclass —
    rendering twice yields identical bytes and the envelope cannot mutate."""
    env = _canonical_envelope()
    assert E.render_bytes(env) == E.render_bytes(env)
    with pytest.raises(dc.FrozenInstanceError):
        env.payload = ("mutated",)  # type: ignore[misc]


def test_tito_law5_chain_determinism_and_tamper():
    """LAW 5 HASH-CHAIN: deterministic; reorder + omission + boundary-shift change
    the terminal hash; a malformed parent RAISES; genesis == the zero parent."""
    a = E.EvidenceEnvelope.build(producer="p", fact_id="a", target="src/a.py",
                                 evidence_type="t", payload=("A",),
                                 provenance=(("src/a.py", 1),))
    b = E.EvidenceEnvelope.build(producer="p", fact_id="b", target="src/b.py",
                                 evidence_type="t", payload=("B",),
                                 provenance=(("src/b.py", 2),))
    ra, rb = E.render_bytes(a), E.render_bytes(b)
    h_ab = E.chain_hash(E.chain_hash("", ra), rb)
    h_ba = E.chain_hash(E.chain_hash("", rb), ra)
    h_a = E.chain_hash("", ra)
    assert h_ab == E.chain_hash(E.chain_hash("", ra), rb)   # deterministic
    assert h_ab != h_ba                                     # reorder
    assert h_ab != h_a                                      # omission
    with pytest.raises(ValueError):
        E.chain_hash("nothex", ra)                          # malformed parent
    assert E.chain_hash("", ra) == E.chain_hash("0" * 64, ra)  # genesis == zero


def test_tito_law10_render_byte_identity_in_process():
    """LAW 10 DETERMINISM: an identical envelope renders byte-identical (sorted keys,
    fixed separators, no set/time/randomness). Cross-PROCESS byte-identity is proven
    in test_replay_battery; here the in-process invariant."""
    env1 = _canonical_envelope()
    env2 = _canonical_envelope()
    assert E.render_bytes(env1) == E.render_bytes(env2)
    assert E.content_signature(env1.payload, env1.provenance) == \
        E.content_signature(env2.payload, env2.provenance)


# ===========================================================================
# Dedup canonical derivation — the F1 collision + freshness lesson
# (home: evidence_envelope.derive_dedup_key + content_signature).
# ===========================================================================
def test_dedup_distinct_symbols_same_first_file_do_not_collide():
    """F1: two DISTINCT symbols whose def sets share the alphabetically-first file
    have DIFFERENT dedup keys (fact_id separates identities)."""
    sig = E.content_signature(("x",), (("core/base.py", 1),))
    k_run = E.derive_dedup_key("gw", "def_ref_partition", "core/base.py", "run", sig)
    k_handle = E.derive_dedup_key("gw", "def_ref_partition", "core/base.py", "handle", sig)
    assert k_run != k_handle


def test_dedup_freshness_and_idempotence():
    """The SAME fact re-fires when its def-set CHANGES (content_signature differs) and
    stays suppressed when identical (freshness + idempotent dedup)."""
    fresh = E.content_signature(("x",), (("core/base.py", 1),))
    moved = E.content_signature(("x",), (("core/base.py", 99),))
    same = E.content_signature(("x",), (("core/base.py", 1),))
    assert fresh != moved       # a moved def re-fires
    assert fresh == same        # an identical repeat is suppressed


# ===========================================================================
# UNMEASURED => ADVISORY across THREE surfaces — blocking requires measured/proof.
# ===========================================================================
def test_unmeasured_advisory_envelope():
    """Envelope surface (evidence_envelope.validate law c): an unmeasured envelope
    cannot carry an enforcing blocking_eligibility."""
    v = _rekey(_canonical_envelope(), blocking_eligibility=E.ONE_BOUNCE, measured=False)
    assert any(x.startswith("authority:") for x in E.validate(v))


def test_unmeasured_advisory_verification_plan():
    """VerificationPlan surface (verification_plan.green, F5 attribution honesty): a
    PASS whose reach is NOT structurally proven (closure/convention basis,
    attribution_satisfied=False) is 'attribution_unmet' — an advisory pass, NEVER an
    enforcing green."""
    plan = VP.VerificationPlan(patch_revision="p1", graph_revision="g1",
                               changed_entities=("foo",), obligations=(), checks=(),
                               edited_files=("src/foo.py",))
    res = VP.CheckResult(
        kind="unit", selection_basis=VP._BASIS_CLOSURE, executed=True, verdict="pass",
        graph_revision="g1", patch_revision="p1", covered_entities=("foo",),
        covered_obligations=(), attribution_requirement="edit_attributed",
        attribution_satisfied=False)
    verdict = VP.green(res, plan)
    assert verdict.green is False and verdict.status == "attribution_unmet"


def test_unmeasured_advisory_hypothesis_ledger():
    """HypothesisLedger surface (hypothesis_ledger.advisory_violations): a recovery
    candidate is ALWAYS advisory — a non-advisory blocking_eligibility (or a VERIFIED
    tier) is a violation."""
    blocking = HL.Advisory(transition=HL.T_ENV_FAILURE,
                           disposition=HL.D_REPAIR_NOT_SOURCE, tier=HL.INFO,
                           blocking_eligibility=E.BLOCKING, statement="the run is env.")
    assert any(x.startswith("authority:") for x in HL.advisory_violations(blocking))
    verified = HL.Advisory(transition=HL.T_ENV_FAILURE,
                           disposition=HL.D_REPAIR_NOT_SOURCE, tier=E.VERIFIED,
                           blocking_eligibility=E.ADVISORY, statement="the run is env.")
    assert any(x.startswith("tier:") for x in HL.advisory_violations(verified))


# ===========================================================================
# T2 LAW — obligations NEVER gate a submit (home: submit_gate.build_certificate).
# ===========================================================================
def test_t2_obligations_never_gate():
    """T2 REGRESSION LAW (measured 4/4): all obligations UNMET (FAIL) with a clean
    head still ALLOWS; obligation_coverage is excluded from every block-bearing
    rollup (unresolved_failures / unknowns)."""
    cert = SG.build_certificate(
        covering={"verdict": "pass"},
        obligations={"unmet": ["o1", "o2"], "total": 3},
        submit_revision="r1")
    assert cert.obligation_coverage.status == SG.FAIL     # obligations ARE unmet
    assert cert.decision == "allow"                        # ...but they do not gate
    assert "obligation_coverage" not in cert.unresolved_failures
    assert "obligation_coverage" not in cert.unknowns


def test_t2_certificate_unknown_fails_open():
    """FAIL-OPEN complement: an UNKNOWN SDLC field is VISIBLE but never blocks —
    decision stays 'allow' on a clean head (submit_gate._derive_decision)."""
    cert = SG.build_certificate(covering=None, submit_revision="r1")  # covering UNKNOWN
    assert "selected_test_status" in cert.unknowns
    assert cert.decision == "allow"


# ===========================================================================
# GREEN LAW — exit-0-unmapped + phantom-entity + stale rejected + true green
# (home: verification_plan.green).
# ===========================================================================
def _plan_and_result(**res_overrides):
    plan = VP.VerificationPlan(patch_revision="p1", graph_revision="g1",
                               changed_entities=("foo",), obligations=(), checks=(),
                               edited_files=("src/foo.py",))
    base = dict(kind="unit", selection_basis=VP._BASIS_FACT, executed=True,
                verdict="pass", graph_revision="g1", patch_revision="p1",
                covered_entities=("foo",), covered_obligations=(),
                attribution_requirement="edit_attributed", attribution_satisfied=True)
    base.update(res_overrides)
    return plan, VP.CheckResult(**base)


def test_green_law_true_green():
    """A fact-basis PASS that is executed, fresh, attributed, and covers a plan-named
    entity is GREEN (verification_plan.green — the affirmative)."""
    plan, res = _plan_and_result()
    assert VP.green(res, plan) == VP.GreenVerdict(True, "green", "unit")


def test_green_law_exit0_unmapped():
    """A PASS covering NOTHING the plan named is 'executed_unmapped', never green
    (verification_plan.green — a bare exit-0 with no coverage mapping)."""
    plan, res = _plan_and_result(covered_entities=())
    assert VP.green(res, plan).status == "executed_unmapped"


def test_green_law_phantom_entity():
    """A PASS claiming coverage of an entity the plan never named is
    'executed_unmapped' (verification_plan.green F4 membership)."""
    plan, res = _plan_and_result(covered_entities=("phantom",))
    assert VP.green(res, plan).status == "executed_unmapped"


def test_green_law_stale_rejected():
    """A PASS stamped with a revision != the plan's is 'stale', never green
    (verification_plan.green freshness)."""
    plan, res = _plan_and_result(graph_revision="OTHERrev")
    assert VP.green(res, plan).status == "stale"


# ===========================================================================
# EpisodeState — single dedup owner + reset semantics
# (home: runtime/episode_state.py; owner delegation via runtime/gateway.py).
# ===========================================================================
def test_episode_single_dedup_owner():
    """SINGLE-OWNER: GatewayState.delivered_keys IS episode.delivered_dedup — one
    storage owner (a mutation through the facade is visible on the episode)."""
    st = GW.GatewayState(graph_db=None, repo_root="")
    assert st.delivered_keys is st.episode.delivered_dedup
    st.delivered_keys.add("k1")
    assert "k1" in st.episode.delivered_dedup


def test_episode_reset_clears_accumulators_persists_identity():
    """reset_attempt clears the per-attempt accumulators to a fresh slate while
    PERSISTING episode_id + step_limit (episode_state.reset_attempt)."""
    e = ES.EpisodeState(episode_id="ep1", step_limit=150)
    e.action_index = 7
    e.edited_files["src/x.py"] = 3
    e.delivered_dedup.add("k")
    e.test_count = 2
    e.reset_attempt()
    assert e.action_index == 0 and not e.edited_files and not e.delivered_dedup
    assert e.test_count == 0
    # D-11: reset_attempt preserves the BASE identity + budget, adds the attempt suffix
    assert e.episode_id == "ep1#a1" and e.step_limit == 150


def test_episode_round_trip_law():
    """from_dict(to_dict(e)) == e on value fields — the deterministic, wallclock-free
    round trip (episode_state.to_dict / from_dict)."""
    e = ES.EpisodeState(episode_id="ep", step_limit=50)
    e.action_index = 4
    e.edited_files["a.py"] = 1
    e.delivered_dedup.update({"x", "y"})
    e.note_edited_lines("a.py", [(1, 3)])
    assert ES.EpisodeState.from_dict(e.to_dict()) == e


# ===========================================================================
# Mutation registry integrity — every handle points at a real module + test
# (import-check only, no auto-mutation). Home: tests/contract/mutation_registry.py.
# ===========================================================================
def test_mutation_registry_references_real_modules_and_tests():
    """The mutation registry cannot rot: every entry's module file exists, every
    import_path resolves, and every biting-test file exists."""
    problems = mutation_registry.validate_registry()
    assert problems == [], problems


def test_mutation_registry_indexes_every_named_wave_handle():
    """The registry indexes the full named-handle set from the enrichment ledger
    (envelope/gateway/episode/grader/planner/certificate/ledger/overlay/miner/
    corpus/indexer). Guards against a silently dropped handle."""
    ids = {m["id"] for m in mutation_registry.list_mutations()}
    expected = {
        "envelope_chain_parent", "envelope_chain_width", "envelope_totality",
        "gateway_dedup", "gateway_belt", "gateway_production_stamping",
        "episode_second_owner", "episode_reset",
        "grader_boundary", "grader_no_further_search", "grader_strict",
        "grader_cd_prefix",
        "planner_conf_gate", "planner_freshness",
        "certificate_unknowns_block", "certificate_obligation_gates",
        "ledger_novelty", "ledger_falsification",
        "overlay_baseline_blame", "overlay_rollback", "overlay_revision",
        "miner_multi_intent", "corpus_split_guard",
        "indexer_edge_hash", "indexer_summary_field",
        # ENDGAME-1 CONFIRMED-2 — ledger-named handles P7-E had missed.
        "change_surface_min_siblings", "change_surface_min_signals",
        "change_surface_independent_signal",
        "patch_delta_registration_shape", "patch_delta_conf_floor",
        "indexer_strip_receiver_type", "seam_m1_byte_preservation",
        "certificate_freshness", "certificate_bulkhead",
        "overlay_attestation_guard",
        # ENDGAME-3 — mini-seam lattice/guard handles (FIX-1..FIX-4).
        "guard_search_exclusion", "guard_search_totality",
        "lattice_hits_fallthrough_mark", "lattice_compound_gate",
    }
    assert expected <= ids, sorted(expected - ids)


def test_endgame_confirmed2_handles_registered_and_executable():
    """ENDGAME-1 CONFIRMED-2 closure: the ten ledger-named biting mutations P7-E
    had missed are all registered, the registry validates clean, and any
    non-Python handle carries a re-run command (so 'gates re-run every named
    mutation mechanically' is TRUE, not ~70% true)."""
    by_id = {m["id"]: m for m in mutation_registry.list_mutations()}
    endgame = {
        "change_surface_min_siblings", "change_surface_min_signals",
        "change_surface_independent_signal",
        "patch_delta_registration_shape", "patch_delta_conf_floor",
        "indexer_strip_receiver_type", "seam_m1_byte_preservation",
        "certificate_freshness", "certificate_bulkhead",
        "overlay_attestation_guard",
    }
    assert endgame <= set(by_id), sorted(endgame - set(by_id))
    assert mutation_registry.validate_registry() == []
    # the Go strip-law handle is not pytest-runnable -> it must name a verify_command.
    go = by_id["indexer_strip_receiver_type"]
    assert go.get("language") == "go"
    assert go.get("verify_command"), "go handle must carry a re-run command"
    # every endgame handle records an honest mechanical-proof status.
    for hid in endgame:
        assert by_id[hid].get("run_status"), f"{hid} missing run_status"
