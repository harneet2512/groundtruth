from __future__ import annotations

import pytest

from groundtruth.runtime.fact_registry import REGISTRY
from groundtruth.runtime.feature_lineage import (
    CAP_BYTE_OWNER_MECHANISMS,
    CAP_BYTE_OWNER_IDS,
    CAP_ELIGIBILITY_IDS,
    CAP_FEATURE_IDS,
    LINEAGE_SCHEMA,
    DeliveryLineage,
    FeatureRef,
    build_lineage,
    cap_role_for,
    lineage_ledger_extra,
    lineage_to_dict,
)


def test_every_canonical_fact_gets_exact_fact_identity() -> None:
    for fact_class, registration in REGISTRY.items():
        lineage = build_lineage(
            runtime_producer_id=registration.producer,
            evidence_type=fact_class,
            actual_event=registration.deliver_by,
        )
        assert lineage is not None
        assert lineage.fact_class == fact_class
        assert lineage.registered_producer_id == registration.producer
        assert lineage.producer_registration_match is True
        assert lineage.required_event == registration.deliver_by
        assert lineage.receipt_predicate == registration.receipt_predicate
        assert lineage.features == (FeatureRef("FACT", fact_class, "fact"),)
        assert lineage.causal_eval == registration.causal_eval
        assert lineage.causal_probe_id == ""
        assert lineage.causal_contribution_proven is False


def test_fine_alias_keeps_runtime_identity_and_explicit_cap_owner() -> None:
    lineage = build_lineage(
        runtime_producer_id="patch_delta",
        evidence_type="signature_mismatch",
        actual_event="edit_result",
        cap_feature_ids=("GT_PATCH_DELTA", "GT_PATCH_DELTA"),
    )
    assert lineage is not None
    assert lineage.schema == LINEAGE_SCHEMA
    assert lineage.evidence_type == "signature_mismatch"
    assert lineage.fact_class == "signature_delta"
    assert lineage.features == (
        FeatureRef("CAP", "GT_PATCH_DELTA", "byte_owner"),
        FeatureRef("FACT", "signature_delta", "fact"),
    )


def test_test_result_obligation_has_registered_boundary_lineage() -> None:
    lineage = build_lineage(
        runtime_producer_id="spec",
        evidence_type="obligation_unexercised",
        actual_event="test_result",
    )
    assert lineage is not None
    assert lineage.producer_registration_match is True
    assert lineage.fact_class == "obligations"
    assert lineage.required_event == "test_result"
    assert lineage.features == (FeatureRef("FACT", "obligations", "fact"),)


def test_coherence_v2_is_an_explicit_recovery_producer() -> None:
    lineage = build_lineage(
        runtime_producer_id="ss_coherence_v2",
        evidence_type="coherence_collapse",
        actual_event="edit_result",
    )
    assert lineage is not None
    assert lineage.runtime_producer_id == "ss_coherence_v2"
    assert lineage.registered_producer_id == "governor"
    assert lineage.producer_registration_match is True
    assert lineage.fact_class == "recovery"
    assert lineage.required_event == "edit_result"

    mismatch = build_lineage(
        runtime_producer_id="coherence_by_name_only",
        evidence_type="coherence_collapse",
        actual_event="edit_result",
    )
    assert mismatch is not None
    assert mismatch.producer_registration_match is False


def test_dynamic_alias_keeps_fine_evidence_type() -> None:
    lineage = build_lineage(
        runtime_producer_id="change_surface",
        evidence_type="missing_role:handler",
        actual_event="edit_result",
        cap_feature_ids=("GT_CHANGE_SURFACE",),
    )
    assert lineage is not None
    assert lineage.evidence_type == "missing_role:handler"
    assert lineage.fact_class == "newfile_precedent"


def test_unregistered_fact_fails_closed() -> None:
    assert build_lineage(
        runtime_producer_id="mystery",
        evidence_type="not_registered",
        actual_event="edit_result",
    ) is None


def test_runtime_producer_claim_cannot_establish_registered_ownership() -> None:
    lineage = build_lineage(
        runtime_producer_id="caller_claim",
        evidence_type="signature_mismatch",
        actual_event="edit_result",
    )
    assert lineage is not None
    assert lineage.registered_producer_id == "patch_delta"
    assert lineage.producer_registration_match is False
    assert lineage_ledger_extra(lineage)["producer_registration_match"] is False


def test_cap_owner_cannot_claim_another_producers_bytes() -> None:
    with pytest.raises(ValueError, match="not authorized"):
        build_lineage(
            runtime_producer_id="patch_delta",
            evidence_type="signature_mismatch",
            actual_event="edit_result",
            cap_feature_ids=("GT_CHANGE_SURFACE",),
        )


def test_lineage_rejects_multiple_cap_byte_owners() -> None:
    with pytest.raises(ValueError, match="at most one"):
        build_lineage(
            runtime_producer_id="patch_delta",
            evidence_type="signature_mismatch",
            actual_event="edit_result",
            cap_feature_ids=("GT_PATCH_DELTA", "GT_CHANGE_SURFACE"),
        )


@pytest.mark.parametrize(
    ("evidence_type", "producer"),
    [
        ("localization", "ranked_localization"),
        ("def_ref_partition", "def_ref_partition"),
        ("wrong_surface", "wrong_surface"),
        ("name_fold", "name_fold"),
        ("body_concept", "body_concept"),
        ("new_file_destination", "change_surface"),
        ("missing_role:handler", "change_surface"),
        ("trace_frame", "trace"),
        ("signature_mismatch", "patch_delta"),
        ("companion_surface", "patch_delta"),
        ("caller_break", "caller_contract"),
        ("covering_verdict", "covering"),
    ],
)
def test_fine_gateway_producers_are_authoritative(
    evidence_type: str, producer: str
) -> None:
    lineage = build_lineage(
        runtime_producer_id=producer,
        evidence_type=evidence_type,
        actual_event="search_result",
    )
    assert lineage is not None
    assert lineage.producer_registration_match is True


def test_lineage_serialization_is_deterministic_and_ledger_safe() -> None:
    lineage = build_lineage(
        runtime_producer_id="patch_delta",
        evidence_type="signature_mismatch",
        actual_event="edit_result",
        cap_feature_ids=("GT_PATCH_DELTA",),
    )
    assert lineage is not None
    payload = lineage_to_dict(lineage)
    assert payload["features"] == [
        {"category": "CAP", "feature_id": "GT_PATCH_DELTA", "role": "byte_owner"},
        {"category": "FACT", "feature_id": "signature_delta", "role": "fact"},
    ]
    extra = lineage_ledger_extra(lineage)
    assert extra["lineage_schema"] == LINEAGE_SCHEMA
    assert extra["feature_ids"] == payload["features"]
    assert extra["runtime_producer_id"] == "patch_delta"


@pytest.mark.parametrize(
    ("category", "feature_id"),
    [("", "x"), ("UNKNOWN", "x"), ("CAP", ""), ("CAP", "not_a_cap")],
)
def test_malformed_feature_refs_fail_loudly(category: str, feature_id: str) -> None:
    with pytest.raises(ValueError):
        FeatureRef(category, feature_id, "byte_owner" if category == "CAP" else "fact")


def test_direct_lineage_rejects_mismatched_fact_ref() -> None:
    with pytest.raises(ValueError, match="FACT feature"):
        DeliveryLineage(
            schema=LINEAGE_SCHEMA,
            runtime_producer_id="patch_delta",
            registered_producer_id="patch_delta",
            producer_registration_match=True,
            evidence_type="signature_mismatch",
            fact_class="caller_contract",
            features=(FeatureRef("FACT", "covering_red", "fact"),),
            required_event="edit_result",
            actual_event="edit_result",
            receipt_predicate="preserved_caller_contract",
            causal_eval="paired_contract_preservation_delta",
            causal_probe_id="",
            causal_contribution_proven=False,
            reactive=False,
        )


def test_undeclared_cap_byte_owner_fails_closed() -> None:
    with pytest.raises(ValueError, match="not a declared byte owner"):
        build_lineage(
            runtime_producer_id="x",
            evidence_type="signature_mismatch",
            actual_event="edit_result",
            cap_feature_ids=("GT_NOT_AN_OWNER",),
        )


def test_cap_role_authority_is_total_and_has_exact_byte_owners() -> None:
    assert len(CAP_FEATURE_IDS) == 47
    assert CAP_BYTE_OWNER_IDS == {
        "GT_EDIT_CHECK", "GT_CHANGE_SURFACE", "GT_PATCH_DELTA", "GT_HYPOTHESIS",
        "GT_LOC_RESLOT", "GT_SS_COHERENCE_V2", "GT_SS_SUBMIT_RED", "GT_CERT_DELIVERY",
    }
    assert CAP_ELIGIBILITY_IDS == {
        "GT_OBLIGATION_FRESHNESS", "GT_SS_NOVELTY", "GT_SS_EXEC_TRUTH",
        "GT_EDIT_OVERLAY", "GT_SS_RECOVERY_V2", "GT_BRIEF_MINIMAL",
        "GT_REGISTRY_ENFORCE", "GT_SS_DEDUP2", "GT_SS_ELIGIBILITY",
        "GT_XSESSION_MEMORY", "GT_D7_RELATEDNESS", "GT_SS_SHADOW",
        "GT_SS_LATE_DROP",
    }
    assert {cap_role_for(feature_id) for feature_id in CAP_FEATURE_IDS} == {
        "byte_owner", "mediator", "eligibility"
    }
    assert cap_role_for("GT_SS_ELIGIBILITY") == "eligibility"
    assert cap_role_for("GT_GLOBAL_ARBITER") == "mediator"


def test_cap_byte_owner_mechanism_authority_is_total_and_exact() -> None:
    assert set(CAP_BYTE_OWNER_MECHANISMS) == set(CAP_BYTE_OWNER_IDS)
    assert {
        feature_id: authority.mechanism
        for feature_id, authority in CAP_BYTE_OWNER_MECHANISMS.items()
    } == {
        "GT_CHANGE_SURFACE": "typed_lineage",
        "GT_PATCH_DELTA": "typed_lineage",
        "GT_LOC_RESLOT": "typed_lineage",
        "GT_SS_SUBMIT_RED": "typed_lineage",
        "GT_EDIT_CHECK": "exact_profile_member",
        "GT_HYPOTHESIS": "exact_profile_member",
        "GT_SS_COHERENCE_V2": "exact_profile_member",
        "GT_CERT_DELIVERY": "exact_profile_member",
    }
    coherence = CAP_BYTE_OWNER_MECHANISMS["GT_SS_COHERENCE_V2"]
    assert coherence.bindings[0].producer == "ss_coherence_v2"
    assert coherence.bindings[0].layer == "detect.coherence"
    assert coherence.bindings[0].fact_class == "recovery"
