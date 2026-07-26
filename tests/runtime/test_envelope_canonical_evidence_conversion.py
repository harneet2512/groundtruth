"""Wave-6 RED contract for envelope/lineage -> canonical evidence conversion.

FACT is the physical evidence identity.  CAP byte owners remain audit lineage
on that same evidence object; they never create additional evidence or appear
in the model-facing capsule.
"""
from __future__ import annotations

from dataclasses import replace

from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import fact_registry
from groundtruth.runtime import feature_lineage
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

FACT_IDS = frozenset(
    feature_id
    for feature_id, registration in fact_registry.REGISTRY.items()
    if registration.fact_role == fact_registry.FACT_ROLE_DELIVERY
)
CAP_OWNER_IDS = feature_lineage.CAP_BYTE_OWNER_IDS

OWNER_FACT = {
    "GT_CHANGE_SURFACE": "newfile_precedent",
    "GT_PATCH_DELTA": "signature_delta",
    "GT_LOC_RESLOT": "localization",
    "GT_SS_SUBMIT_RED": "submit_refusal",
    "GT_EDIT_CHECK": "syntax_result",
    "GT_HYPOTHESIS": "recovery",
    "GT_CERT_DELIVERY": "submit_refusal",
}

OWNER_EVIDENCE_TYPE = {
    "GT_CHANGE_SURFACE": "missing_role",
    "GT_PATCH_DELTA": "companion_surface",
    "GT_LOC_RESLOT": "localization",
    "GT_SS_SUBMIT_RED": "submit_refusal",
    "GT_EDIT_CHECK": "syntax_result",
    "GT_HYPOTHESIS": "recovery",
    "GT_CERT_DELIVERY": "submit_refusal",
}


def _semantics(fact_class: str):
    contract = rr.feature_contract_for(fact_class)
    assert contract is not None
    return rr.CanonicalEvidenceSemantics(
        decision_context=contract.decision_context,
        roles=contract.roles,
        claim=f"Structured claim for {fact_class}.",
        actionable_consequence=f"Structured consequence for {fact_class}.",
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            f"subject:{fact_class}",
        ),
        authority=rr.Authority.RESULT_DERIVED,
        revision=REVISION,
        revision_dependencies=contract.revision_dependencies,
        mandatory_reason=None,
        failure_prevention=5,
        causal_value=4,
        contradiction_resolution=0,
        anchoring_risk=0,
    )


def _lineage(
    fact_class: str,
    *,
    owner_ids=(),
    runtime_producer_id: str | None = None,
    evidence_type: str | None = None,
):
    registration = fact_registry.REGISTRY[fact_class]
    producer = runtime_producer_id or registration.producer
    layer = evidence_type or fact_class
    required = fact_registry.required_event(layer)
    assert required is not None
    lineage = feature_lineage.build_lineage(
        runtime_producer_id=producer,
        evidence_type=layer,
        actual_event=required,
    )
    assert lineage is not None
    features = {
        feature_lineage.FeatureRef("FACT", fact_class, "fact"),
        *(
            feature_lineage.FeatureRef("CAP", owner_id, "byte_owner")
            for owner_id in owner_ids
        ),
    }
    return replace(lineage, features=tuple(sorted(features)))


def _envelope(
    fact_class: str,
    *,
    owner_ids=(),
    runtime_producer_id: str | None = None,
    evidence_type: str | None = None,
    lineage_override=...,
    tier: str = ee.VERIFIED,
):
    registration = fact_registry.REGISTRY[fact_class]
    producer = runtime_producer_id or registration.producer
    layer = evidence_type or fact_class
    lineage = (
        _lineage(
            fact_class,
            owner_ids=owner_ids,
            runtime_producer_id=producer,
            evidence_type=layer,
        )
        if lineage_override is ...
        else lineage_override
    )
    return ee.EvidenceEnvelope.build(
        producer=producer,
        fact_id=f"physical:{fact_class}",
        target=f"src/product/{fact_class}.py::subject",
        evidence_type=layer,
        payload=("unrendered producer payload",),
        provenance=((f"src/product/{fact_class}.py", 17),),
        confidence=0.91,
        tier=tier,
        graph_revision=REVISION.graph,
        valid_until=REVISION.graph,
        preferred_event=ee.EVENT_VIEW,
        estimated_cost_tokens=40,
        lineage=lineage,
        producer_inputs={"legacy": "structured computation input"},
        canonical_semantics=_semantics(fact_class),
    )


def _owner_envelope(owner_id: str):
    return _envelope(
        OWNER_FACT[owner_id],
        owner_ids=(owner_id,),
        evidence_type=OWNER_EVIDENCE_TYPE[owner_id],
    )


def test_exact_fact_and_cap_owner_universe_maps_to_ten_physical_records() -> None:
    assert len(FACT_IDS) == 10
    assert len(CAP_OWNER_IDS) == 7

    owner_by_fact = {
        fact_class: tuple(
            sorted(
                owner_id
                for owner_id, owner_fact in OWNER_FACT.items()
                if owner_fact == fact_class
            )
        )
        for fact_class in FACT_IDS
    }
    envelopes = tuple(
        _envelope(
            fact_class,
            owner_ids=owner_by_fact[fact_class],
            evidence_type=(
                OWNER_EVIDENCE_TYPE[owner_by_fact[fact_class][0]]
                if owner_by_fact[fact_class]
                else fact_class
            ),
        )
        for fact_class in sorted(FACT_IDS)
    )

    records = rr.canonicalize_evidence_envelopes(envelopes)

    assert len(records) == 10
    assert {record.feature_id for record in records} == FACT_IDS
    assert {
        owner
        for record in records
        for owner in record.owner_feature_ids
    } == CAP_OWNER_IDS
    assert all(record.feature_id not in record.owner_feature_ids for record in records)


def test_duplicate_physical_envelopes_merge_owner_lineage_not_evidence() -> None:
    submit_red = _owner_envelope("GT_SS_SUBMIT_RED")
    certificate = _owner_envelope("GT_CERT_DELIVERY")
    assert submit_red.dedup_key == certificate.dedup_key

    records = rr.canonicalize_evidence_envelopes(
        (submit_red, certificate, submit_red)
    )

    assert len(records) == 1
    assert records[0].feature_id == "submit_refusal"
    assert records[0].owner_feature_ids == (
        "GT_CERT_DELIVERY",
        "GT_SS_SUBMIT_RED",
    )


def test_canonical_fact_identity_comes_only_from_authoritative_lineage() -> None:
    envelope = _envelope(
        "signature_delta",
        owner_ids=("GT_PATCH_DELTA",),
        evidence_type="companion_surface",
    )

    record = rr.canonical_evidence_from_envelope(envelope)

    assert record is not None
    assert record.feature_id == "signature_delta"
    assert record.feature_id == envelope.lineage.fact_class
    assert record.feature_id != envelope.evidence_type
    assert record.owner_feature_ids == ("GT_PATCH_DELTA",)


def test_conversion_preserves_tier_authority_provenance_revision_and_contract() -> None:
    envelope = _envelope("caller_contract", tier=ee.WARNING)
    semantics = envelope.canonical_semantics
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None

    record = rr.canonical_evidence_from_envelope(envelope)

    assert record is not None
    assert record.grade is rr.EvidenceGrade.WARNING
    assert record.authority is rr.Authority.RESULT_DERIVED
    assert record.provenance == ("src/product/caller_contract.py:17",)
    assert record.revision == REVISION
    assert record.decision_context is contract.decision_context
    assert record.roles == contract.roles
    assert record.revision_dependencies == contract.revision_dependencies
    assert record.claim == semantics.claim
    assert record.actionable_consequence == semantics.actionable_consequence


def test_required_roles_and_consequence_come_from_structured_semantics() -> None:
    for fact_class in sorted(FACT_IDS):
        envelope = _envelope(fact_class)
        record = rr.canonical_evidence_from_envelope(envelope)
        contract = rr.feature_contract_for(fact_class)
        semantics = envelope.canonical_semantics

        assert record is not None
        assert contract is not None
        assert record.roles == contract.roles == semantics.roles
        assert record.actionable_consequence == semantics.actionable_consequence
        assert "unrendered producer payload" not in record.actionable_consequence


def test_unknown_or_missing_lineage_is_correctly_quiet() -> None:
    unknown = ee.EvidenceEnvelope.build(
        producer="unknown",
        fact_id="physical:unknown",
        target="src/product/unknown.py::subject",
        evidence_type="unknown_type",
        payload=("unknown",),
        provenance=(("src/product/unknown.py", 1),),
        confidence=0.9,
        tier=ee.VERIFIED,
        graph_revision=REVISION.graph,
        preferred_event=ee.EVENT_VIEW,
        canonical_semantics=_semantics("caller_contract"),
        lineage=None,
    )

    assert rr.canonical_evidence_from_envelope(unknown) is None
    assert rr.canonicalize_evidence_envelopes((unknown,)) == ()


def test_mismatched_or_unauthorized_producer_is_correctly_quiet() -> None:
    mismatched_lineage = _lineage(
        "caller_contract",
        runtime_producer_id="not-the-registered-producer",
    )
    assert mismatched_lineage.producer_registration_match is False
    unauthorized = _envelope(
        "caller_contract",
        runtime_producer_id="not-the-registered-producer",
        lineage_override=mismatched_lineage,
    )

    assert rr.canonical_evidence_from_envelope(unauthorized) is None

    valid = _envelope("caller_contract")
    foreign_lineage = _lineage("localization")
    crossed = replace(valid, lineage=foreign_lineage)
    assert rr.canonical_evidence_from_envelope(crossed) is None


def test_model_facing_capsule_contains_no_feature_metadata_fields() -> None:
    record = rr.canonical_evidence_from_envelope(
        _envelope(
            "newfile_precedent",
            owner_ids=("GT_CHANGE_SURFACE",),
            evidence_type="missing_role",
        )
    )
    assert record is not None
    record = replace(record, lifecycle=rr.EvidenceLifecycle.READY)
    decision = rr.ActiveDecision(
        decision_id="new-file",
        context=record.decision_context,
        primary_claim="Use the repository integration precedent.",
        required_roles=record.roles,
        causal_neighborhood=record.causal_neighborhood,
        token_budget=220,
        current_revision=REVISION,
    )
    oracle = rr.select_evidence_coalition(decision, (record,))
    compiled = rr.compile_observation_capsule(
        native_observation="native",
        decision=oracle,
        observation_id="obs-1",
        source_model_call_id="call-0",
        model_call_id="call-1",
        enabled=True,
    )

    assert compiled.state is rr.CapsuleCompilationState.COMPILED
    # Repository paths/symbols may legitimately equal a FACT identifier; the
    # compiler must preserve those bytes rather than performing substring
    # replacement. Internal identities leak only when rendered as metadata.
    assert "src/product/newfile_precedent.py:17" in compiled.capsule_text
    assert "feature_id:" not in compiled.capsule_text.lower()
    assert "producer:" not in compiled.capsule_text.lower()
    assert all(
        owner_id not in compiled.capsule_text
        for owner_id in record.owner_feature_ids
    )
