from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.attestation_store import persist_attestation
from groundtruth.runtime.gateway_attestation_factory import (
    build_gateway_attestation,
    canonical_producer_inputs_bytes,
)
from groundtruth.runtime.producer_attestation import PASS, UNMEASURED, validate
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    ProducerInputs,
    SignatureChange,
    SourceState,
)

_SHIPPED_BYTES = b"\nexact final model-visible suffix"
_DELIVERY_SEAL = hashlib.sha256(_SHIPPED_BYTES).hexdigest()[:16]


def _source(file: str, token: str) -> SourceState:
    return SourceState(file=file, sha256=token * 64, revision=f"source:{token * 64}")


def _envelope(evidence_type: str = "caller_break") -> EvidenceEnvelope:
    producer = "caller_contract" if evidence_type == "caller_break" else "patch_delta"
    env = EvidenceEnvelope.build(
        producer=producer,
        fact_id="get_user",
        target="src/api.py",
        evidence_type=evidence_type,
        payload=("exact existing payload",),
        provenance=(("src/caller.py", 2),),
        graph_revision="graph-9",
    )
    if evidence_type == "caller_break":
        change = SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=("uid",), after_parameters=("uid", "name"),
            old_min_params=None, old_max_params=None,
            new_min_params=None, new_max_params=None, positional_args=None,
        )
    else:
        change = SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=None, after_parameters=None,
            old_min_params=1, old_max_params=1,
            new_min_params=2, new_max_params=2, positional_args=1,
        )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type=evidence_type,
        candidate_id=env.dedup_key,
        before_state=_source("src/api.py", "a"),
        after_state=_source("src/api.py", "b"),
        caller_rows=(CallerEvidenceRow(
            identity="use", file="src/caller.py", line=2, confidence=0.95,
            resolution_method="import", source_state=_source("src/caller.py", "c"),
            edge_id=17, definition_id=4,
        ),),
        graph_revision="graph-9",
        signature_changes=(change,),
    )
    return dataclasses.replace(env, producer_inputs=inputs)


@pytest.mark.parametrize(
    ("evidence_type", "fact_class", "registered_producer"),
    [
        ("caller_break", "caller_contract", "contract_map"),
        ("signature_mismatch", "signature_delta", "patch_delta"),
    ],
)
def test_factory_builds_valid_pass_attestation_and_exact_artifact_map(
    evidence_type: str, fact_class: str, registered_producer: str
) -> None:
    envelope = _envelope(evidence_type)

    attestation, artifacts = build_gateway_attestation(
        envelope,
        delivery_seal=_DELIVERY_SEAL,
        shipped_bytes=_SHIPPED_BYTES,
        actual_event="edit_result",
        open_event="edit_result",
    )

    assert validate(attestation) == ()
    assert attestation.truth_verdict == PASS
    assert attestation.freshness_verdict == PASS
    assert attestation.evidence_type == evidence_type
    assert attestation.runtime_producer_id == envelope.producer
    assert attestation.registered_producer_id == registered_producer
    assert attestation.candidate_id == envelope.dedup_key
    assert attestation.delivery_seal == _DELIVERY_SEAL
    assert attestation.decision.required_event == "edit_result"
    assert attestation.decision.open_event == "edit_result"
    assert any(ref.kind == "producer_inputs" for ref in attestation.source_artifacts)
    assert all(
        hashlib.sha256(artifacts[ref.artifact_id]).hexdigest() == ref.sha256
        for ref in attestation.source_artifacts
    )
    assert all(
        ":" not in ref.artifact_id for ref in attestation.source_artifacts
    )
    rendered_ref = next(
        ref for ref in attestation.source_artifacts
        if ref.kind == "rendered_candidate"
    )
    assert artifacts[rendered_ref.artifact_id] == _SHIPPED_BYTES
    assert {
        proof.field_path
        for proof in attestation.truth_predicates[0].proof_refs
        if proof.proof_type == "producer_input"
    } == {"$.caller_rows", "$.signature_changes"}
    rendered_proofs = [
        proof for proof in attestation.truth_predicates[0].proof_refs
        if proof.proof_type == "rendered_candidate"
    ]
    assert len(rendered_proofs) == 1
    assert rendered_proofs[0].artifact == rendered_ref
    assert rendered_proofs[0].field_path == f"bytes[0:{len(_SHIPPED_BYTES)}]"
    freshness_paths = {
        proof.field_path
        for proof in attestation.freshness_predicates[0].proof_refs
        if proof.proof_type == "producer_input"
    }
    assert "$.before_state.sha256" in freshness_paths
    assert "$.after_state.revision" in freshness_paths
    assert "$.caller_rows[0].source_state.sha256" in freshness_paths
    assert "$.graph_revision" in freshness_paths
    payload = b"".join(artifacts[key] for key in sorted(artifacts))
    assert fact_class.encode() in payload
    assert b'"edge_id":17' in payload
    assert b'"definition_id":4' in payload
    assert b'"graph_revision":"graph-9"' in payload


def test_canonical_input_bytes_bind_events_sources_rows_and_semantics() -> None:
    envelope = _envelope("caller_break")
    raw = canonical_producer_inputs_bytes(
        envelope,
        delivery_seal="e" * 16,
        actual_event="edit_result",
        open_event="edit_result",
    )

    assert raw == canonical_producer_inputs_bytes(
        envelope,
        delivery_seal="e" * 16,
        actual_event="edit_result",
        open_event="edit_result",
    )
    assert b'"candidate_id":"' + envelope.dedup_key.encode() + b'"' in raw
    assert b'"before_parameters":["uid"]' in raw
    assert b'"after_parameters":["uid","name"]' in raw
    assert b'"actual_event":"edit_result"' in raw
    assert b'"open_event":"edit_result"' in raw


def test_incomplete_semantic_or_source_inputs_are_unmeasured_not_pass() -> None:
    envelope = _envelope("signature_mismatch")
    inputs = envelope.producer_inputs
    incomplete_change = dataclasses.replace(
        inputs.signature_changes[0], positional_args=None
    )
    incomplete_row = dataclasses.replace(
        inputs.caller_rows[0], edge_id=None, source_state=None
    )
    incomplete = dataclasses.replace(
        inputs,
        signature_changes=(incomplete_change,),
        caller_rows=(incomplete_row,),
    )

    attestation, artifacts = build_gateway_attestation(
        dataclasses.replace(envelope, producer_inputs=incomplete),
        delivery_seal=_DELIVERY_SEAL,
        shipped_bytes=_SHIPPED_BYTES,
        actual_event="edit_result",
        open_event="edit_result",
    )

    assert validate(attestation) == ()
    assert attestation.truth_verdict == UNMEASURED
    assert attestation.freshness_verdict == UNMEASURED
    assert attestation.truth_predicates[0].proof_refs == ()
    assert attestation.freshness_predicates[0].proof_refs == ()
    assert artifacts


def test_missing_graph_edge_identity_alone_prevents_pass() -> None:
    envelope = _envelope("caller_break")
    inputs = envelope.producer_inputs
    incomplete = dataclasses.replace(
        inputs,
        caller_rows=(dataclasses.replace(inputs.caller_rows[0], edge_id=None),),
    )

    attestation, _artifacts = build_gateway_attestation(
        dataclasses.replace(envelope, producer_inputs=incomplete),
        delivery_seal=_DELIVERY_SEAL,
        shipped_bytes=_SHIPPED_BYTES,
        actual_event="edit_result",
        open_event="edit_result",
    )

    assert attestation.truth_verdict == UNMEASURED
    assert attestation.freshness_verdict == UNMEASURED


def test_missing_observed_call_arity_alone_prevents_signature_pass() -> None:
    envelope = _envelope("signature_mismatch")
    inputs = envelope.producer_inputs
    incomplete = dataclasses.replace(
        inputs,
        signature_changes=(dataclasses.replace(
            inputs.signature_changes[0], positional_args=None
        ),),
    )

    attestation, _artifacts = build_gateway_attestation(
        dataclasses.replace(envelope, producer_inputs=incomplete),
        delivery_seal=_DELIVERY_SEAL,
        shipped_bytes=_SHIPPED_BYTES,
        actual_event="edit_result",
        open_event="edit_result",
    )

    assert attestation.truth_verdict == UNMEASURED


@pytest.mark.parametrize("evidence_type", ["companion_surface", "trace_frame"])
def test_factory_fails_closed_for_unsupported_gateway_evidence(evidence_type: str) -> None:
    envelope = dataclasses.replace(
        _envelope("caller_break"), evidence_type=evidence_type
    )
    with pytest.raises(ValueError, match="unsupported"):
        build_gateway_attestation(
            envelope, delivery_seal=_DELIVERY_SEAL,
            shipped_bytes=_SHIPPED_BYTES,
            actual_event="edit_result", open_event="edit_result",
        )


def test_factory_rejects_candidate_producer_seal_and_event_mismatches() -> None:
    envelope = _envelope("caller_break")
    bad_candidate = dataclasses.replace(
        envelope.producer_inputs, candidate_id="wrong-candidate"
    )
    with pytest.raises(ValueError, match="candidate"):
        build_gateway_attestation(
            dataclasses.replace(envelope, producer_inputs=bad_candidate),
            delivery_seal=_DELIVERY_SEAL,
            shipped_bytes=_SHIPPED_BYTES,
            actual_event="edit_result", open_event="edit_result",
        )
    with pytest.raises(ValueError, match="producer"):
        build_gateway_attestation(
            dataclasses.replace(envelope, producer="generic_auditor"),
            delivery_seal=_DELIVERY_SEAL,
            shipped_bytes=_SHIPPED_BYTES,
            actual_event="edit_result", open_event="edit_result",
        )
    with pytest.raises(ValueError, match="seal"):
        build_gateway_attestation(
            envelope, delivery_seal="BAD",
            shipped_bytes=_SHIPPED_BYTES,
            actual_event="edit_result", open_event="edit_result",
        )
    with pytest.raises(ValueError, match="event"):
        build_gateway_attestation(
            envelope, delivery_seal=_DELIVERY_SEAL,
            shipped_bytes=_SHIPPED_BYTES,
            actual_event="", open_event="edit_result",
        )


def test_factory_rejects_seal_that_does_not_name_exact_shipped_bytes() -> None:
    with pytest.raises(ValueError, match="does not match shipped bytes"):
        build_gateway_attestation(
            _envelope("caller_break"),
            delivery_seal=hashlib.sha256(b"different bytes").hexdigest()[:16],
            shipped_bytes=_SHIPPED_BYTES,
            actual_event="edit_result",
            open_event="edit_result",
        )


def test_factory_artifact_map_is_directly_persistable(
    tmp_path: Path,
) -> None:
    attestation, artifacts = build_gateway_attestation(
        _envelope("caller_break"),
        delivery_seal=_DELIVERY_SEAL,
        shipped_bytes=_SHIPPED_BYTES,
        actual_event="edit_result",
        open_event="edit_result",
    )

    stored = persist_attestation(attestation, artifacts, tmp_path)

    assert stored.bundle_dir.is_dir()
    rendered_ref = next(
        ref for ref in attestation.source_artifacts
        if ref.kind == "rendered_candidate"
    )
    assert (
        stored.bundle_dir / "artifacts" / rendered_ref.artifact_id
    ).read_bytes() == _SHIPPED_BYTES
