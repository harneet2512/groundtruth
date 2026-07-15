"""Pure factory for producer-owned Gateway truth/freshness attestations.

The factory consumes a *final delivered* envelope and its render-neutral
``ProducerInputs`` sidecar. It performs no I/O and does not promote SS-LIVE. Only
the two Gateway producer pairs whose semantic inputs are currently complete are
supported: caller_break/caller_contract and
signature_mismatch/signature_delta. Source-state artifacts are canonical
producer-observation descriptors (file/hash/revision), not source-file bytes;
the exact final model-visible bytes are stored separately as rendered_candidate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .evidence_envelope import EvidenceEnvelope
from .fact_registry import EVENTS, producer_matches, registration_for, required_event
from .producer_attestation import (
    ATTESTATION_SCHEMA,
    FRESHNESS,
    PASS,
    TRUTH,
    UNMEASURED,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
    validate,
)
from .producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    ProducerInputs,
    SignatureChange,
    SourceState,
)


_SUPPORTED: dict[str, str] = {
    "caller_break": "caller_contract",
    "signature_mismatch": "signature_delta",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _source_dict(state: SourceState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "file": state.file,
        "sha256": state.sha256,
        "revision": state.revision,
    }


def _caller_dict(row: CallerEvidenceRow) -> dict[str, Any]:
    return {
        "identity": row.identity,
        "file": row.file,
        "line": row.line,
        "confidence": row.confidence,
        "resolution_method": row.resolution_method,
        "edge_id": row.edge_id,
        "definition_id": row.definition_id,
        "source_state": _source_dict(row.source_state),
    }


def _change_dict(change: SignatureChange) -> dict[str, Any]:
    return {
        "symbol": change.symbol,
        "edited_file": change.edited_file,
        "before_parameters": (
            list(change.before_parameters)
            if change.before_parameters is not None else None
        ),
        "after_parameters": (
            list(change.after_parameters)
            if change.after_parameters is not None else None
        ),
        "old_min_params": change.old_min_params,
        "old_max_params": change.old_max_params,
        "new_min_params": change.new_min_params,
        "new_max_params": change.new_max_params,
        "positional_args": change.positional_args,
    }


def _input_payload(
    envelope: EvidenceEnvelope,
    *,
    delivery_seal: str,
    actual_event: str,
    open_event: str,
) -> dict[str, Any]:
    inputs = envelope.producer_inputs
    if not isinstance(inputs, ProducerInputs):
        raise ValueError("producer inputs missing or wrong type")
    registration = registration_for(envelope.evidence_type)
    if registration is None:
        raise ValueError("unsupported unregistered evidence type")
    return {
        "schema": "gt.gateway_attestation_inputs.v1",
        "producer_inputs_schema": inputs.schema,
        "evidence_type": envelope.evidence_type,
        "fact_class": registration.fact_class,
        "candidate_id": inputs.candidate_id,
        "delivery_seal": delivery_seal,
        "runtime_producer_id": envelope.producer,
        "registered_producer_id": registration.producer,
        "actual_event": actual_event,
        "open_event": open_event,
        "required_event": required_event(envelope.evidence_type),
        "before_state": _source_dict(inputs.before_state),
        "after_state": _source_dict(inputs.after_state),
        "caller_rows": [_caller_dict(row) for row in inputs.caller_rows],
        "graph_revision": inputs.graph_revision,
        "signature_changes": [
            _change_dict(change) for change in inputs.signature_changes
        ],
    }


def canonical_producer_inputs_bytes(
    envelope: EvidenceEnvelope,
    *,
    delivery_seal: str,
    actual_event: str,
    open_event: str,
) -> bytes:
    """Canonical bytes binding every producer input and delivery/event identity."""

    return _canonical_json(
        _input_payload(
            envelope,
            delivery_seal=delivery_seal,
            actual_event=actual_event,
            open_event=open_event,
        )
    )


def _valid_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _complete_source(state: SourceState | None) -> bool:
    return bool(
        isinstance(state, SourceState)
        and state.file.strip()
        and _valid_sha(state.sha256)
        and state.revision.strip()
    )


def _complete_caller(row: CallerEvidenceRow) -> bool:
    return bool(
        row.identity.strip()
        and row.file.strip()
        and isinstance(row.line, int)
        and not isinstance(row.line, bool)
        and row.line > 0
        and isinstance(row.confidence, (int, float))
        and not isinstance(row.confidence, bool)
        and 0.7 <= float(row.confidence) <= 1.0
        and isinstance(row.resolution_method, str)
        and row.resolution_method.strip()
        and isinstance(row.edge_id, int)
        and not isinstance(row.edge_id, bool)
        and row.edge_id > 0
        and isinstance(row.definition_id, int)
        and not isinstance(row.definition_id, bool)
        and row.definition_id > 0
        and _complete_source(row.source_state)
    )


def _caller_change_complete(change: SignatureChange) -> bool:
    return bool(
        change.symbol.strip()
        and change.edited_file.strip()
        and isinstance(change.before_parameters, tuple)
        and isinstance(change.after_parameters, tuple)
        and change.before_parameters != change.after_parameters
        and all(isinstance(item, str) and item for item in change.before_parameters)
        and all(isinstance(item, str) and item for item in change.after_parameters)
        and all(
            value is None
            for value in (
                change.old_min_params, change.old_max_params,
                change.new_min_params, change.new_max_params,
                change.positional_args,
            )
        )
    )


def _signature_change_complete(change: SignatureChange) -> bool:
    values = (
        change.old_min_params, change.old_max_params,
        change.new_min_params, change.new_max_params, change.positional_args,
    )
    if not (
        change.symbol.strip()
        and change.edited_file.strip()
        and change.before_parameters is None
        and change.after_parameters is None
        and all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    ):
        return False
    old_min, old_max, new_min, new_max, positional = values
    return bool(
        0 <= old_min <= old_max
        and 0 <= new_min <= new_max
        and positional >= 0
        and old_min <= positional <= old_max
        and not (new_min <= positional <= new_max)
    )


def _artifact_bundle(
    envelope: EvidenceEnvelope,
    payload_bytes: bytes,
    shipped_bytes: bytes,
) -> tuple[tuple[ArtifactRef, ...], dict[str, bytes]]:
    inputs = envelope.producer_inputs
    assert isinstance(inputs, ProducerInputs)
    candidate = inputs.candidate_id
    artifacts: dict[str, bytes] = {}
    refs: list[ArtifactRef] = []

    def add(artifact_id: str, kind: str, revision: str, raw: bytes) -> None:
        artifacts[artifact_id] = raw
        refs.append(ArtifactRef(
            kind=kind,
            artifact_id=artifact_id,
            sha256=hashlib.sha256(raw).hexdigest(),
            revision=revision,
        ))

    identity = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    add(
        f"producer-inputs-{identity}.json",
        "producer_inputs",
        f"candidate:{candidate}",
        payload_bytes,
    )
    add(
        f"rendered-candidate-{identity}.bin",
        "rendered_candidate",
        f"delivery:{hashlib.sha256(shipped_bytes).hexdigest()[:16]}",
        shipped_bytes,
    )
    states: list[tuple[str, SourceState | None]] = [
        ("before", inputs.before_state), ("after", inputs.after_state),
    ]
    states.extend(
        (f"caller:{index}", row.source_state)
        for index, row in enumerate(inputs.caller_rows)
    )
    for index, (role, state) in enumerate(states):
        if not _complete_source(state):
            continue
        assert state is not None
        raw = _canonical_json({"role": role, **_source_dict(state)})
        add(f"source-state-{identity}-{index}.json", "source_state", state.revision, raw)
    return tuple(sorted(refs)), artifacts


def _predicate(
    *,
    kind: str,
    predicate_id: str,
    subject: str,
    expectation: str,
    complete: bool,
    producer_input_ref: ArtifactRef,
    rendered_ref: ArtifactRef,
    rendered_length: int,
    field_paths: tuple[str, ...],
) -> PredicateAttestation:
    proof_refs = tuple(sorted((
        *(
            ProofRef(
                proof_type="producer_input",
                artifact=producer_input_ref,
                field_path=field_path,
            )
            for field_path in field_paths
        ),
        ProofRef(
            proof_type="rendered_candidate",
            artifact=rendered_ref,
            field_path=f"bytes[0:{rendered_length}]",
        ),
    ))) if complete else ()
    return PredicateAttestation(
        predicate_kind=kind,
        predicate_id=predicate_id,
        subject=subject,
        expectation=expectation,
        observation="complete producer-owned structured inputs" if complete else "",
        verdict=PASS if complete else UNMEASURED,
        proof_refs=proof_refs,
    )


def build_gateway_attestation(
    envelope: EvidenceEnvelope,
    *,
    delivery_seal: str,
    shipped_bytes: bytes,
    actual_event: str,
    open_event: str,
) -> tuple[ProducerAttestation, dict[str, bytes]]:
    """Build one validated producer attestation plus exact artifact bytes."""

    expected_fact_class = _SUPPORTED.get(envelope.evidence_type)
    if expected_fact_class is None:
        raise ValueError(f"unsupported Gateway evidence type: {envelope.evidence_type!r}")
    inputs = envelope.producer_inputs
    if not isinstance(inputs, ProducerInputs):
        raise ValueError("producer inputs missing or wrong type")
    registration = registration_for(envelope.evidence_type)
    if registration is None or registration.fact_class != expected_fact_class:
        raise ValueError("unsupported registry fact-class binding")
    if not producer_matches(envelope.evidence_type, envelope.producer):
        raise ValueError("producer is not authoritative for evidence type")
    if inputs.schema != PRODUCER_INPUTS_SCHEMA:
        raise ValueError("producer inputs schema mismatch")
    if inputs.evidence_type != envelope.evidence_type:
        raise ValueError("producer inputs evidence type mismatch")
    if inputs.candidate_id != envelope.dedup_key:
        raise ValueError("producer inputs candidate mismatch")
    if not (
        isinstance(delivery_seal, str)
        and len(delivery_seal) == 16
        and all(char in "0123456789abcdef" for char in delivery_seal)
    ):
        raise ValueError("delivery seal must be exactly 16 lowercase hex")
    if not isinstance(shipped_bytes, bytes) or not shipped_bytes:
        raise ValueError("shipped bytes must be nonempty exact bytes")
    if hashlib.sha256(shipped_bytes).hexdigest()[:16] != delivery_seal:
        raise ValueError("delivery seal does not match shipped bytes")
    if actual_event not in EVENTS or open_event not in EVENTS:
        raise ValueError("actual/open event must be a registered fine event")

    raw = canonical_producer_inputs_bytes(
        envelope,
        delivery_seal=delivery_seal,
        actual_event=actual_event,
        open_event=open_event,
    )
    source_refs, artifacts = _artifact_bundle(envelope, raw, shipped_bytes)
    producer_input_ref = next(
        ref for ref in source_refs if ref.kind == "producer_inputs"
    )
    rendered_ref = next(
        ref for ref in source_refs if ref.kind == "rendered_candidate"
    )
    common_complete = bool(
        _complete_source(inputs.before_state)
        and _complete_source(inputs.after_state)
        and inputs.caller_rows
        and all(_complete_caller(row) for row in inputs.caller_rows)
        and inputs.graph_revision
        and inputs.graph_revision == envelope.graph_revision
    )
    if envelope.evidence_type == "caller_break":
        semantic_complete = bool(
            len(inputs.signature_changes) == 1
            and _caller_change_complete(inputs.signature_changes[0])
        )
    else:
        semantic_complete = bool(
            len(inputs.signature_changes) == 1
            and _signature_change_complete(inputs.signature_changes[0])
        )
    truth_complete = common_complete and semantic_complete
    freshness_complete = common_complete and semantic_complete
    truth_paths = ("$.caller_rows", "$.signature_changes")
    freshness_paths = (
        "$.before_state.sha256",
        "$.before_state.revision",
        "$.after_state.sha256",
        "$.after_state.revision",
        "$.graph_revision",
        *tuple(
            path
            for index in range(len(inputs.caller_rows))
            for path in (
                f"$.caller_rows[{index}].source_state.sha256",
                f"$.caller_rows[{index}].source_state.revision",
            )
        ),
    )

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=envelope.evidence_type,
        runtime_producer_id=envelope.producer,
        registered_producer_id=registration.producer,
        candidate_id=envelope.dedup_key,
        delivery_seal=delivery_seal,
        source_artifacts=source_refs,
        truth_predicates=(_predicate(
            kind=TRUTH,
            predicate_id=f"{envelope.evidence_type}.semantic_truth",
            subject=envelope.dedup_key,
            expectation="structured producer inputs prove the emitted semantic change",
            complete=truth_complete,
            producer_input_ref=producer_input_ref,
            rendered_ref=rendered_ref,
            rendered_length=len(shipped_bytes),
            field_paths=truth_paths,
        ),),
        freshness_predicates=(_predicate(
            kind=FRESHNESS,
            predicate_id=f"{envelope.evidence_type}.candidate_freshness",
            subject=envelope.dedup_key,
            expectation="source and graph revisions bind the final candidate",
            complete=freshness_complete,
            producer_input_ref=producer_input_ref,
            rendered_ref=rendered_ref,
            rendered_length=len(shipped_bytes),
            field_paths=freshness_paths,
        ),),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            open_event=open_event,
            required_event=required_event(envelope.evidence_type) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        raise ValueError("invalid producer attestation: " + ";".join(errors))
    return attestation, artifacts


__all__ = [
    "build_gateway_attestation",
    "canonical_producer_inputs_bytes",
]
