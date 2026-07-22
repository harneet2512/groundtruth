from __future__ import annotations

import hashlib
from types import SimpleNamespace

import gt_mini_patch as g
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.feature_lineage import build_lineage


def _env(*, producer="patch_delta", evidence_type="signature_mismatch"):
    return EvidenceEnvelope.build(
        producer=producer, fact_id="Widget", target="src/widget.py",
        evidence_type=evidence_type, payload=("final fact",),
        provenance=(("src/widget.py", 4),), confidence=0.9,
        tier="VERIFIED", preferred_event="edit",
    )


def test_gateway_final_controls_share_exact_observation_seal(monkeypatch):
    rows = []
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    final = "\nfinal model-facing bytes"
    env = _env()

    g._record_gateway_final_controls(
        env, final, native=True, globally_arbitrated=True,
        provenance_decision="NO_EFFECT")

    controls = {row["control_ref"]["feature_id"]: row for row in rows}
    # B-GW (2026-07-16): the gateway mediator join records a GT_GATEWAY row with the
    # COMMITTED-delivery identity (gt_mini_patch._record_gateway_final_controls ->
    # gateway.record_committed_delivery) so the admission-time row's pre-render seal
    # mismatch no longer breaks the exact join. It must share the exact seal below.
    assert set(controls) == {
        "GT_GATEWAY", "GT_GATEWAY_NATIVE", "GT_GLOBAL_ARBITER", "GT_SS_ARBITER_V2",
        "GT_SS_PROVENANCE", "GT_INSEAM_METRICS",
    }
    seal = hashlib.sha256(final.encode()).hexdigest()[:16]
    assert all(row["candidate_sha256_16"] == seal for row in controls.values())
    assert all(row["candidate_id"] == env.dedup_key for row in controls.values())
    assert all(row["fact_class"] == "signature_delta" for row in controls.values())
    assert g._gateway_delivery_extra(env)["candidate_id"] == env.dedup_key


def test_loc_reslot_attaches_exact_typed_byte_owner():
    env = _env(producer="ranked_localization", evidence_type="localization")
    owned = g._attach_exact_gateway_byte_owner(env, "search")
    refs = {(ref.category, ref.feature_id, ref.role) for ref in owned.lineage.features}
    assert ("CAP", "GT_LOC_RESLOT", "byte_owner") in refs
    assert owned.lineage.fact_class == "localization"


def test_unrelated_gateway_candidate_gets_no_loc_reslot_owner():
    env = _env()
    assert g._attach_exact_gateway_byte_owner(env, "edit") is env


def test_coherence_has_exact_single_owner_at_delivery(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    assert g._lane_profile_member_extra("detect.coherence") == {
        "profile_member": "GT_SS_COHERENCE_V2"
    }


def test_lane_delivery_lineage_is_registered_and_never_inferred_from_arbiter(monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    edit = g._lane_delivery_extra(
        "edit.syntax", "syntax is valid", "src/widget.py", g.Event.POST_EDIT)
    assert edit["profile_member"] == "GT_EDIT_CHECK"
    assert edit["runtime_producer_id"] == "edit_check"
    assert edit["registered_producer_id"] == "edit_check"
    assert edit["producer_registration_match"] is True
    assert edit["fact_class"] == "syntax_result"
    assert edit["actual_event"] == "edit_result"
    assert edit["feature_ids"] == [
        {"category": "FACT", "feature_id": "syntax_result", "role": "fact"}
    ]

    legacy = g._lane_delivery_extra(
        "l3.contract", "legacy contract", "src/widget.py", g.Event.POST_VIEW)
    assert legacy["candidate_id"]
    # Typed-lineage expansion (2026-07-16+, 234b2c3e6/8ad8e63c6): legacy lane rows now
    # carry the SAME registered typed lineage as the primary rows — fact_class is stamped
    # from the registry (never inferred from the arbiter), which is exactly what this
    # test's name demands. Pin the registered value.
    assert legacy["fact_class"] == "caller_contract"
    assert legacy["lineage_schema"]


def test_coherence_delivery_retains_owner_without_fabricated_fact_lineage(
    monkeypatch,
):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    extra = g._lane_delivery_extra(
        "detect.coherence", "two verified writes", "src/widget.py", g.Event.POST_EDIT)
    assert extra["profile_member"] == "GT_SS_COHERENCE_V2"
    assert extra["candidate_id"]
    assert "fact_class" not in extra
    assert "lineage_schema" not in extra
    assert "feature_ids" not in extra


def test_reactive_lane_kinds_carry_only_exact_supported_lineage(monkeypatch):
    cases = {
        "obligation.unexercised": ("spec", "obligations", "test_result"),
        "detect.loop": ("governor", "recovery", "failure_obs"),
    }
    for kind, (producer, fact_class, actual_event) in cases.items():
        extra = g._lane_delivery_extra(
            kind, "exact sealed bytes", "src/widget.py",
            g.Event.TEST_RESULT,
        )
        assert extra["runtime_producer_id"] == producer
        assert extra["registered_producer_id"] == producer
        assert extra["producer_registration_match"] is True
        assert extra["fact_class"] == fact_class
        assert extra["actual_event"] == actual_event
        assert extra["feature_ids"] == [
            {"category": "FACT", "feature_id": fact_class, "role": "fact"}
        ]

    monkeypatch.setattr(
        g, "_last_verify_executed_identity",
        ("covering_runner", "covering_red", "test_result"))
    executed = g._lane_delivery_extra(
        "verify.horizon.executed", "covering RED", "src/widget.py",
        g.Event.REVIEW_TRANSITION)
    assert executed["fact_class"] == "covering_red"
    # B-BND (b2, c9ac61d3c): the SEAM's executed covering RED delivers synchronously at
    # the EDIT boundary, so the registry overrides required_event to edit_result while the
    # seam still stamps the canonical test_result actual label (reactive: on-time by
    # construction — fact_registry.py:679-712).
    assert executed["actual_event"] == "test_result"
    assert executed["required_event"] == "edit_result"

    monkeypatch.setattr(g, "_last_verify_executed_identity", None)
    for unsupported_kind in (
        "consensus.scope", "consensus.scope_map", "verify.horizon.advisory",
        "verify.horizon.urgent", "verify.horizon.pivot", "verify.horizon.gate",
        "arbitrary.scope-looking-name",
    ):
        unsupported = g._lane_delivery_extra(
            unsupported_kind, "exact bytes", "src/widget.py", g.Event.POST_VIEW)
        assert "fact_class" not in unsupported
        assert "lineage_schema" not in unsupported


def test_reactive_steer_candidate_binds_fact_opportunity(monkeypatch):
    pool = []
    rows = []
    monkeypatch.setattr(g, "_last_gate_winner_kind", "detect.loop")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    monkeypatch.setattr(g, "_ss_any_content_gate_on", lambda: False)
    monkeypatch.setattr(
        g, "_append_batch_candidate",
        lambda target, candidate, thunk, *args, **kwargs:
            target.append((candidate, thunk)),
    )
    monkeypatch.setenv("GT_STEER_NATIVE", "0")

    g._global_pool_add_steer(
        pool, {"output": "base"}, "pytest", "break the repeated loop",
        kkind="", kf="", krel="src/widget.py", event=g.Event.TEST_RESULT,
        steer_base="base",
    )

    assert len(pool) == 1
    candidate = pool[0][0]
    assert candidate.lineage.fact_class == "recovery"
    assert candidate.lineage.runtime_producer_id == "governor"

    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    state = {
        "observation_id": "1" * 64,
        "parent_policy_sha256": "2" * 64,
        "parent_policy_chars": 4,
        "action_batch_sha256": "3" * 64,
        "batch_start_iteration": 1,
        "pool": pool,
    }
    g._record_batch_opportunities(
        state, {id(candidate): SimpleNamespace(disposition="deliver")}, candidate)
    assert rows[0]["attribution_status"] == "BOUND"
    assert rows[0]["feature_refs"] == [
        {"category": "FACT", "feature_id": "recovery", "role": "fact"}
    ]


def test_reactive_lineage_is_observation_byte_neutral(monkeypatch):
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kwargs: None)
    monkeypatch.setattr(g, "_last_gate_winner_kind", "detect.loop")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "0")

    def deliver() -> bytes:
        out = {"output": "base"}
        g._deliver_gate_winner(
            out, "pytest", "break the repeated loop", kkind="", kf="",
            krel="src/widget.py", event=g.Event.TEST_RESULT,
            steer_base="base")
        return out["output"].encode()

    with_lineage = deliver()
    monkeypatch.setattr(g, "_LANE_REGISTERED_PRODUCERS", {})
    without_lineage = deliver()
    assert with_lineage == without_lineage == b"base\nbreak the repeated loop"


def test_covering_execution_stamps_concrete_lineage_identity(tmp_path, monkeypatch):
    import groundtruth.runtime.covering_runner as covering_runner
    import groundtruth.runtime.native_render as native_render

    test_file = tmp_path / "covering.py"
    test_file.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_test_step", None)
    monkeypatch.setattr(g, "_action_count", 4)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_build_verification_executor", lambda: None)
    monkeypatch.setattr(
        covering_runner, "run_covering_tests",
        lambda *args, **kwargs: {"verdict": "fail", "ran": ["covering.py"]},
    )
    monkeypatch.setattr(
        covering_runner, "attribute_covering_red", lambda *args, **kwargs:
        covering_runner.CoveringAttribution(
            attributed=True,
            method="differential",
            current_verdict="fail",
            base_verdict="pass",
            implicated_edited_paths=("src/widget.py",),
            covering_files=("covering.py",),
        ))
    monkeypatch.setattr(
        native_render, "render_covering_failure_native",
        lambda *args, **kwargs: "covering failure",
    )

    block = g._executed_covering_emission(
        [{"file": "covering.py"}], {"src/widget.py"}, {"widget"})
    assert block == "covering failure"
    assert g._last_verify_executed_identity == (
        "covering_runner", "covering_red", "test_result")
    lineage = g._lane_registered_lineage(
        "verify.horizon.executed", g.Event.REVIEW_TRANSITION)
    assert lineage.fact_class == "covering_red"
    # B-BND (b2): required_event overridden to edit_result (see fact_registry.py:690);
    # the canonical actual label stays test_result and reactive treatment keeps it on-time.
    assert lineage.actual_event == "test_result"
    assert lineage.required_event == "edit_result"


def test_submit_red_has_authorized_typed_byte_owner():
    lineage = build_lineage(
        runtime_producer_id="submit_gate", evidence_type="submit_refusal",
        actual_event="submit", cap_feature_ids=("GT_SS_SUBMIT_RED",))
    refs = {(ref.category, ref.feature_id, ref.role) for ref in lineage.features}
    assert ("CAP", "GT_SS_SUBMIT_RED", "byte_owner") in refs
    assert lineage.producer_registration_match is True


def test_cross_plane_lane_and_steer_winners_use_final_bytes(monkeypatch):
    rows = []
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    for kind in ("l3.contract", "recovery"):
        g._record_cross_plane_final_controls(
            SimpleNamespace(kind=kind, dedup_key="id-" + kind), "\ncommitted " + kind)
    globals_ = [r for r in rows if r["control_ref"]["feature_id"] ==
                "GT_GLOBAL_ARBITER"]
    assert len(globals_) == 2
    assert {r["fact_class"] for r in globals_} == {"caller_contract", "recovery"}


def test_nonbatch_gateway_has_no_global_arbiter_receipt(monkeypatch):
    rows = []
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    g._record_gateway_final_controls(
        _env(), "final", native=False, globally_arbitrated=False)
    assert all(r["control_ref"]["feature_id"] != "GT_GLOBAL_ARBITER" for r in rows)


def test_lane_provenance_outcomes_are_exact(monkeypatch):
    rows = []
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    for decision, final in (("APPLIED", "clean"), ("SUPPRESSED", ""),
                            ("NO_EFFECT", "already clean")):
        g._record_lane_provenance_control(
            "l3.contract", "original", final, "src/widget.py", decision)
    assert [r["participation_decision"] for r in rows] == [
        "APPLIED", "SUPPRESSED", "NO_EFFECT"]
    assert rows[0]["candidate_sha256_16"] == hashlib.sha256(b"clean").hexdigest()[:16]
    assert rows[0]["candidate_id"] == g._lane_final_extra(
        "l3.contract", "clean", "src/widget.py")["candidate_id"]
    # SUPPRESSED withholds the candidate, so the record seals the PRE-suppression bytes
    # (original_text) — never the empty final. Sealing nothing (chars 0) would fail the
    # referee's _has_identity guard and drop the record, laundering the suppression into
    # an unverifiable claim; the pre-suppression seal keeps the exact `not _carried`
    # absence check byte-verifiable (the same contract the SUPPRESSED grader consumes).
    assert rows[1]["candidate_sha256_16"] == hashlib.sha256(b"original").hexdigest()[:16]
    assert rows[1]["candidate_chars"] == len("original")
    assert rows[1]["candidate_id"] == g._lane_final_extra(
        "l3.contract", "original", "src/widget.py")["candidate_id"]


def test_runtime_ledger_accepts_string_and_enum_event_contract(monkeypatch):
    rows = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    g._runtime_ledger_record(kind="lane", outcome="eligible", event="post_search")
    g._runtime_ledger_record(kind="lane", outcome="eligible", event=g.Event.POST_EDIT)
    assert [row["event_type"] for row in rows] == ["post_search", "post_edit"]
