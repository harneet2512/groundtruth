"""Pure producer-owned truth and freshness attestations.

This module defines an immutable audit sidecar for a final evidence candidate.  It
does not inspect repositories, read artifacts, render text, or decide SS-LIVE.  Its
job is narrower: bind a registered producer's explicit, typed claims to the exact
candidate, source revisions, decision boundary, and delivered-byte seal that a live
auditor will later join.

The contract deliberately has no ``truth_valid``/``freshness_valid`` booleans.
PASS and FAIL require exact proof references; absence of measurement is represented
as UNMEASURED and cannot carry proof that would imply a result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .fact_registry import EVENTS, producer_matches, registration_for, required_event


ATTESTATION_SCHEMA = "gt.producer_attestation.v1"

TRUTH = "TRUTH"
FRESHNESS = "FRESHNESS"
PREDICATE_KINDS = frozenset({TRUTH, FRESHNESS})

PASS = "PASS"
FAIL = "FAIL"
UNMEASURED = "UNMEASURED"
VERDICTS = frozenset({PASS, FAIL, UNMEASURED})

_SHA256_HEX_LEN = 64
_DELIVERY_SEAL_HEX_LEN = 16


def _is_lower_hex(value: object, width: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and all(char in "0123456789abcdef" for char in value)
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True, order=True)
class ArtifactRef:
    """Exact identity of one producer-observed source artifact."""

    kind: str
    artifact_id: str
    sha256: str
    revision: str


@dataclass(frozen=True, order=True)
class ProofRef:
    """A typed location inside an exact source artifact."""

    proof_type: str
    artifact: ArtifactRef
    field_path: str


@dataclass(frozen=True, order=True)
class PredicateAttestation:
    """One producer-owned truth or freshness proposition and its evidence."""

    predicate_kind: str
    predicate_id: str
    subject: str
    expectation: str
    observation: str
    verdict: str
    proof_refs: tuple[ProofRef, ...]


@dataclass(frozen=True, order=True)
class DecisionBinding:
    """The registry decision and chronological event at which it opened."""

    decision_key: str
    open_event: str
    required_event: str


@dataclass(frozen=True)
class ProducerAttestation:
    """Immutable producer claim bound to one final rendered evidence candidate."""

    schema: str
    evidence_type: str
    runtime_producer_id: str
    registered_producer_id: str
    candidate_id: str
    delivery_seal: str
    source_artifacts: tuple[ArtifactRef, ...]
    truth_predicates: tuple[PredicateAttestation, ...]
    freshness_predicates: tuple[PredicateAttestation, ...]
    decision: DecisionBinding

    @property
    def truth_verdict(self) -> str:
        return _aggregate_verdict(self.truth_predicates)

    @property
    def freshness_verdict(self) -> str:
        return _aggregate_verdict(self.freshness_predicates)


def _aggregate_verdict(predicates: tuple[PredicateAttestation, ...]) -> str:
    verdicts = tuple(predicate.verdict for predicate in predicates)
    if FAIL in verdicts:
        return FAIL
    if verdicts and all(verdict == PASS for verdict in verdicts):
        return PASS
    return UNMEASURED


def _validate_artifact(ref: ArtifactRef, prefix: str) -> list[str]:
    errors: list[str] = []
    for field_name in ("kind", "artifact_id", "revision"):
        if not _nonempty_string(getattr(ref, field_name, None)):
            errors.append(f"{prefix}:{field_name}:empty")
    if not _is_lower_hex(getattr(ref, "sha256", None), _SHA256_HEX_LEN):
        errors.append(f"{prefix}:sha256:not_64_lower_hex")
    return errors


def _validate_proof(
    ref: ProofRef,
    prefix: str,
    source_artifacts: tuple[ArtifactRef, ...],
) -> list[str]:
    errors: list[str] = []
    if not _nonempty_string(getattr(ref, "proof_type", None)):
        errors.append(f"{prefix}:proof_type:empty")
    if not _nonempty_string(getattr(ref, "field_path", None)):
        errors.append(f"{prefix}:field_path:empty")
    artifact = getattr(ref, "artifact", None)
    if not isinstance(artifact, ArtifactRef):
        errors.append(f"{prefix}:artifact:wrong_type")
        return errors
    errors.extend(_validate_artifact(artifact, f"{prefix}:artifact"))
    if artifact not in source_artifacts:
        errors.append(f"{prefix}:artifact_not_in_sources")
    return errors


def _validate_predicates(
    predicates: object,
    *,
    expected_kind: str,
    prefix: str,
    source_artifacts: tuple[ArtifactRef, ...],
) -> list[str]:
    errors: list[str] = []
    if not isinstance(predicates, tuple):
        return [f"{prefix}:not_tuple"]
    if not predicates:
        return [f"{prefix}:empty"]
    if not all(isinstance(predicate, PredicateAttestation) for predicate in predicates):
        return [f"{prefix}:wrong_type"]
    if predicates != tuple(sorted(set(predicates))):
        errors.append(f"{prefix}:not_unique_sorted")

    for index, predicate in enumerate(predicates):
        item = f"{prefix}[{index}]"
        if predicate.predicate_kind not in PREDICATE_KINDS:
            errors.append(f"{item}:predicate_kind:unknown")
        elif predicate.predicate_kind != expected_kind:
            errors.append(f"{item}:predicate_kind:mismatch")
        for field_name in ("predicate_id", "subject", "expectation"):
            if not _nonempty_string(getattr(predicate, field_name, None)):
                errors.append(f"{item}:{field_name}:empty")
        if predicate.verdict not in VERDICTS or not isinstance(predicate.verdict, str):
            errors.append(f"{item}:verdict:unknown")
        if not isinstance(predicate.proof_refs, tuple):
            errors.append(f"{item}:proof_refs:not_tuple")
            continue
        if predicate.proof_refs != tuple(sorted(set(predicate.proof_refs))):
            errors.append(f"{item}:proof_refs:not_unique_sorted")
        if predicate.verdict in (PASS, FAIL) and not predicate.proof_refs:
            errors.append(f"{item}:proof_refs:required")
        if predicate.verdict == UNMEASURED and predicate.proof_refs:
            errors.append(f"{item}:proof_refs:forbidden_when_unmeasured")
        if predicate.verdict in (PASS, FAIL) and not _nonempty_string(predicate.observation):
            errors.append(f"{item}:observation:empty")
        for proof_index, proof in enumerate(predicate.proof_refs):
            if not isinstance(proof, ProofRef):
                errors.append(f"{item}:proof_refs[{proof_index}]:wrong_type")
                continue
            errors.extend(
                _validate_proof(
                    proof,
                    f"{item}:proof_refs[{proof_index}]",
                    source_artifacts,
                )
            )
    return errors


def validate(attestation: ProducerAttestation) -> tuple[str, ...]:
    """Return every contract violation in deterministic order; ``()`` is valid.

    Validation is pure.  In particular, it validates exact artifact identities and
    registry authority but does not open those artifacts or infer their contents.
    """

    errors: list[str] = []
    if not isinstance(attestation, ProducerAttestation):
        return ("attestation:wrong_type",)
    if attestation.schema != ATTESTATION_SCHEMA:
        errors.append(f"schema:unsupported:{attestation.schema!r}")

    registration = registration_for(attestation.evidence_type)
    if registration is None:
        errors.append(f"evidence_type:unregistered:{attestation.evidence_type!r}")
    else:
        if not producer_matches(attestation.evidence_type, attestation.runtime_producer_id):
            errors.append(f"producer:unauthorized:{attestation.runtime_producer_id!r}")
        if attestation.registered_producer_id != registration.producer:
            errors.append(
                "registered_producer_id:mismatch:"
                f"{attestation.registered_producer_id!r}!={registration.producer!r}"
            )

    if not _nonempty_string(attestation.candidate_id):
        errors.append("candidate_id:empty")
    if not _is_lower_hex(attestation.delivery_seal, _DELIVERY_SEAL_HEX_LEN):
        errors.append("delivery_seal:not_16_lower_hex")

    source_artifacts = attestation.source_artifacts
    if not isinstance(source_artifacts, tuple):
        errors.append("source_artifacts:not_tuple")
        source_artifacts = ()
    elif not source_artifacts:
        errors.append("source_artifacts:empty")
    elif not all(isinstance(ref, ArtifactRef) for ref in source_artifacts):
        errors.append("source_artifacts:wrong_type")
        source_artifacts = tuple(ref for ref in source_artifacts if isinstance(ref, ArtifactRef))
    if source_artifacts and source_artifacts != tuple(sorted(set(source_artifacts))):
        errors.append("source_artifacts:not_unique_sorted")
    for index, ref in enumerate(source_artifacts):
        errors.extend(_validate_artifact(ref, f"source_artifacts[{index}]"))

    errors.extend(
        _validate_predicates(
            attestation.truth_predicates,
            expected_kind=TRUTH,
            prefix="truth_predicates",
            source_artifacts=source_artifacts,
        )
    )
    errors.extend(
        _validate_predicates(
            attestation.freshness_predicates,
            expected_kind=FRESHNESS,
            prefix="freshness_predicates",
            source_artifacts=source_artifacts,
        )
    )

    decision = attestation.decision
    if not isinstance(decision, DecisionBinding):
        errors.append("decision:wrong_type")
    else:
        if not _nonempty_string(decision.decision_key):
            errors.append("decision:key_empty")
        if decision.open_event not in EVENTS:
            errors.append("decision:open_event_unknown")
        if decision.required_event not in EVENTS:
            errors.append("decision:required_event_unknown")
        if registration is not None:
            if decision.decision_key != registration.target_decision:
                errors.append("decision:key_mismatch")
            if decision.required_event != required_event(attestation.evidence_type):
                errors.append("decision:required_event_mismatch")

    return tuple(errors)


def _artifact_to_dict(ref: ArtifactRef) -> dict[str, str]:
    return {
        "kind": ref.kind,
        "artifact_id": ref.artifact_id,
        "sha256": ref.sha256,
        "revision": ref.revision,
    }


def _proof_to_dict(ref: ProofRef) -> dict[str, Any]:
    return {
        "proof_type": ref.proof_type,
        "artifact": _artifact_to_dict(ref.artifact),
        "field_path": ref.field_path,
    }


def _predicate_to_dict(predicate: PredicateAttestation) -> dict[str, Any]:
    return {
        "predicate_kind": predicate.predicate_kind,
        "predicate_id": predicate.predicate_id,
        "subject": predicate.subject,
        "expectation": predicate.expectation,
        "observation": predicate.observation,
        "verdict": predicate.verdict,
        "proof_refs": [_proof_to_dict(ref) for ref in predicate.proof_refs],
    }


def to_dict(attestation: ProducerAttestation) -> dict[str, Any]:
    """Return the exact JSON-native representation used by the commitment."""

    return {
        "schema": attestation.schema,
        "evidence_type": attestation.evidence_type,
        "runtime_producer_id": attestation.runtime_producer_id,
        "registered_producer_id": attestation.registered_producer_id,
        "candidate_id": attestation.candidate_id,
        "delivery_seal": attestation.delivery_seal,
        "source_artifacts": [_artifact_to_dict(ref) for ref in attestation.source_artifacts],
        "truth_predicates": [
            _predicate_to_dict(predicate) for predicate in attestation.truth_predicates
        ],
        "freshness_predicates": [
            _predicate_to_dict(predicate) for predicate in attestation.freshness_predicates
        ],
        "truth_verdict": attestation.truth_verdict,
        "freshness_verdict": attestation.freshness_verdict,
        "decision": {
            "decision_key": attestation.decision.decision_key,
            "open_event": attestation.decision.open_event,
            "required_event": attestation.decision.required_event,
        },
    }


def canonical_bytes(attestation: ProducerAttestation) -> bytes:
    """Canonical UTF-8 bytes for exact artifact sealing and replay."""

    return json.dumps(
        to_dict(attestation),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(attestation: ProducerAttestation) -> str:
    """SHA-256 of :func:`canonical_bytes`."""

    return hashlib.sha256(canonical_bytes(attestation)).hexdigest()


__all__ = [
    "ATTESTATION_SCHEMA",
    "FAIL",
    "FRESHNESS",
    "PASS",
    "TRUTH",
    "UNMEASURED",
    "ArtifactRef",
    "DecisionBinding",
    "PredicateAttestation",
    "ProducerAttestation",
    "ProofRef",
    "canonical_bytes",
    "canonical_sha256",
    "to_dict",
    "validate",
]
