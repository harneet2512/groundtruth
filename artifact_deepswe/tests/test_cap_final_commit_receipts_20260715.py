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
    assert set(controls) == {
        "GT_GATEWAY_NATIVE", "GT_GLOBAL_ARBITER", "GT_SS_ARBITER_V2",
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
    assert "fact_class" not in legacy
    assert "lineage_schema" not in legacy


def test_coherence_delivery_does_not_fabricate_fact_lineage(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    extra = g._lane_delivery_extra(
        "detect.coherence", "two verified writes", "src/widget.py", g.Event.POST_EDIT)
    assert extra["profile_member"] == "GT_SS_COHERENCE_V2"
    assert extra["candidate_id"]
    assert "fact_class" not in extra
    assert "lineage_schema" not in extra


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
    assert rows[1]["candidate_chars"] == 0


def test_runtime_ledger_accepts_string_and_enum_event_contract(monkeypatch):
    rows = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(row) or True)
    g._runtime_ledger_record(kind="lane", outcome="eligible", event="post_search")
    g._runtime_ledger_record(kind="lane", outcome="eligible", event=g.Event.POST_EDIT)
    assert [row["event_type"] for row in rows] == ["post_search", "post_edit"]
