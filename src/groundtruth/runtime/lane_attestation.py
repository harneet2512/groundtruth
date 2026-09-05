"""Pure final-attestation factories for native lane producers.

Factories bind already-produced host inputs to a caller-supplied candidate id and
final delivery seal.  They never infer a seal, commit a delivery, persist files,
or change model-facing bytes.  Any incomplete join is honestly UNMEASURED.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .covering_runner import CoveringAttribution
from .evidence_envelope import EvidenceEnvelope
from .fact_registry import EVENT_EDIT_RESULT, EVENT_TEST_RESULT, registration_for, required_event
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
from .syntax_observation import SyntaxObservation


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _path_artifact_id(prefix: str, path: str) -> str:
    return f"{prefix}-{_sha(path.encode('utf-8', 'surrogatepass'))}.bin"


def lane_delivery_candidate_id(kind: str, target: str, shipped_suffix: str) -> str:
    """Reproduce the exact final envelope identity owned by `_seal_lane_delivery`."""
    return EvidenceEnvelope.build(
        producer=kind or "lane",
        fact_id="",
        target=target or "",
        evidence_type=kind or "lane",
        payload=(shipped_suffix,),
    ).dedup_key


@dataclass(frozen=True)
class FinalAttestationInputs:
    attestation: ProducerAttestation
    artifacts: tuple[tuple[str, bytes], ...]

    def artifact_mapping(self) -> dict[str, bytes]:
        return dict(self.artifacts)


@dataclass(frozen=True, order=True)
class EditedSourceInput:
    path: str
    sha256: str
    bytes_length: int
    revision: str
    artifact_id: str
    raw_bytes: bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes_length": self.bytes_length,
            "revision": self.revision,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True)
class CoveringCandidateInput:
    attribution: CoveringAttribution
    current_result_bytes: bytes
    current_result_sha256: str
    edited_sources: tuple[EditedSourceInput, ...]
    covering_files: tuple[str, ...]
    actual_event: str
    rendered_sha256_16: str
    rendered_chars: int
    rendered_bytes_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribution": self.attribution.to_dict(),
            "current_result_sha256": self.current_result_sha256,
            "current_result_bytes_length": len(self.current_result_bytes),
            "edited_sources": [source.to_dict() for source in self.edited_sources],
            "covering_files": list(self.covering_files),
            "actual_event": self.actual_event,
            "rendered_sha256_16": self.rendered_sha256_16,
            "rendered_chars": self.rendered_chars,
            "rendered_bytes_length": self.rendered_bytes_length,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_dict())


def build_covering_candidate_input(
    *,
    attribution: CoveringAttribution,
    current_result: Mapping[str, Any],
    edited_sources: Mapping[str, tuple[bytes, str]],
    covering_files: list[str] | tuple[str, ...],
    actual_event: str,
    rendered_block: str,
) -> CoveringCandidateInput:
    """Bind the covering producer's exact result, sources, event, and candidate."""
    current_bytes = _canonical(dict(current_result))
    sources: list[EditedSourceInput] = []
    for raw_path, pair in sorted(edited_sources.items()):
        raw_bytes, revision = pair
        if not isinstance(raw_bytes, bytes):
            raise TypeError("edited source content must be bytes")
        path = str(raw_path).replace("\\", "/")
        sources.append(EditedSourceInput(
            path=path,
            sha256=_sha(raw_bytes),
            bytes_length=len(raw_bytes),
            revision=str(revision or ""),
            artifact_id=_path_artifact_id("edited-source", path),
            raw_bytes=raw_bytes,
        ))
    rendered = rendered_block.encode("utf-8", "surrogatepass")
    return CoveringCandidateInput(
        attribution=attribution,
        current_result_bytes=current_bytes,
        current_result_sha256=_sha(current_bytes),
        edited_sources=tuple(sources),
        covering_files=tuple(sorted({str(path) for path in covering_files if path})),
        actual_event=str(actual_event or ""),
        rendered_sha256_16=_sha(rendered)[:16],
        rendered_chars=len(rendered_block),
        rendered_bytes_length=len(rendered),
    )


def _artifact(
    artifact_id: str, data: bytes, *, kind: str, revision: str
) -> ArtifactRef:
    return ArtifactRef(kind, artifact_id, _sha(data), revision)


def _predicate(
    kind: str,
    predicate_id: str,
    complete: bool,
    proof_refs: tuple[ProofRef, ...],
) -> PredicateAttestation:
    return PredicateAttestation(
        predicate_kind=kind,
        predicate_id=predicate_id,
        subject="exact final lane candidate",
        expectation="producer inputs, current revision, rendered candidate, and event join",
        observation=("complete exact join" if complete else "join incomplete or mismatched"),
        verdict=PASS if complete else UNMEASURED,
        proof_refs=proof_refs if complete else (),
    )


def _final(
    *,
    evidence_type: str,
    runtime_producer_id: str,
    candidate_id: str,
    delivery_seal: str,
    actual_event: str,
    refs: tuple[ArtifactRef, ...],
    artifacts: tuple[tuple[str, bytes], ...],
    complete: bool,
    truth_proofs: tuple[ProofRef, ...],
    freshness_proofs: tuple[ProofRef, ...],
) -> FinalAttestationInputs:
    registration = registration_for(evidence_type)
    if registration is None:
        raise ValueError(f"unregistered evidence type: {evidence_type}")
    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=evidence_type,
        runtime_producer_id=runtime_producer_id,
        registered_producer_id=registration.producer,
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        source_artifacts=tuple(sorted(refs)),
        truth_predicates=(
            _predicate(TRUTH, f"{evidence_type}:truth", complete, tuple(sorted(truth_proofs))),
        ),
        freshness_predicates=(
            _predicate(
                FRESHNESS,
                f"{evidence_type}:freshness",
                complete,
                tuple(sorted(freshness_proofs)),
            ),
        ),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            # These reactive world facts open their decision at the producer-observed
            # event; never fabricate a chronological opening from the static registry.
            open_event=actual_event,
            required_event=required_event(evidence_type) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        raise ValueError("invalid final attestation: " + "|".join(errors))
    return FinalAttestationInputs(attestation, tuple(sorted(artifacts)))


def finalize_syntax_attestation(
    observation: SyntaxObservation,
    *,
    source_bytes: bytes,
    producer_block: str,
    shipped_suffix: str,
    target: str,
    candidate_id: str,
    delivery_seal: str,
) -> FinalAttestationInputs:
    observation_bytes = observation.canonical_json().encode("utf-8")
    rendered_bytes = producer_block.encode("utf-8", "surrogatepass")
    shipped_bytes = shipped_suffix.encode("utf-8", "surrogatepass")
    source_id = _path_artifact_id("edited-source", observation.file_path)
    artifacts = (
        ("syntax-observation.json", observation_bytes),
        ("producer-candidate.bin", rendered_bytes),
        ("shipped-suffix.bin", shipped_bytes),
        (source_id, source_bytes),
    )
    refs = tuple(sorted((
        _artifact(
            "syntax-observation.json", observation_bytes,
            kind="syntax_observation", revision=f"edit:{observation.source_sha256}",
        ),
        _artifact(
            "producer-candidate.bin", rendered_bytes,
            kind="producer_candidate", revision=f"candidate:{candidate_id}",
        ),
        _artifact(
            "shipped-suffix.bin", shipped_bytes,
            kind="shipped_suffix", revision=f"seal:{delivery_seal}",
        ),
        _artifact(
            source_id, source_bytes,
            kind="edited_source", revision=f"edit:{_sha(source_bytes)}",
        ),
    )))
    by_id = {ref.artifact_id: ref for ref in refs}
    complete = (
        observation.verdict == "syntax_error"
        and bool(observation.checker)
        and bool(observation.diagnostic_category)
        and observation.diagnostic_location is not None
        and observation.actual_event == EVENT_EDIT_RESULT
        and observation.source_sha256 == _sha(source_bytes)
        and observation.source_bytes_length == len(source_bytes)
        and observation.rendered_sha256_16 == _sha(rendered_bytes)[:16]
        and observation.rendered_chars == len(producer_block)
        and observation.rendered_bytes_length == len(rendered_bytes)
        and shipped_bytes in (rendered_bytes, b"\n" + rendered_bytes)
        and delivery_seal == _sha(shipped_bytes)[:16]
        and target.replace("\\", "/") == observation.file_path.replace("\\", "/")
        and candidate_id == lane_delivery_candidate_id(
            "edit.syntax", target, shipped_suffix
        )
    )
    return _final(
        evidence_type="syntax_result",
        runtime_producer_id="edit_check",
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        actual_event=observation.actual_event,
        refs=refs,
        artifacts=artifacts,
        complete=complete,
        truth_proofs=(
            ProofRef("producer_observation", by_id["syntax-observation.json"], "$.verdict"),
            ProofRef("producer_render", by_id["producer-candidate.bin"], "$"),
            ProofRef("shipped_identity", by_id["shipped-suffix.bin"], "$"),
        ),
        freshness_proofs=(
            ProofRef("source_revision", by_id[source_id], "$"),
            ProofRef("producer_event", by_id["syntax-observation.json"], "$.actual_event"),
        ),
    )


def finalize_covering_attestation(
    candidate: CoveringCandidateInput,
    *,
    producer_block: str,
    shipped_suffix: str,
    target: str,
    candidate_id: str,
    delivery_seal: str,
) -> FinalAttestationInputs:
    input_bytes = candidate.canonical_bytes()
    rendered_bytes = producer_block.encode("utf-8", "surrogatepass")
    shipped_bytes = shipped_suffix.encode("utf-8", "surrogatepass")
    artifact_rows: list[tuple[str, bytes]] = [
        ("covering-input.json", input_bytes),
        ("current-result.json", candidate.current_result_bytes),
        ("producer-candidate.bin", rendered_bytes),
        ("shipped-suffix.bin", shipped_bytes),
    ]
    refs: list[ArtifactRef] = [
        _artifact(
            "covering-input.json", input_bytes,
            kind="covering_input", revision=f"candidate:{candidate_id}",
        ),
        _artifact(
            "current-result.json", candidate.current_result_bytes,
            kind="test_result", revision=f"result:{candidate.current_result_sha256}",
        ),
        _artifact(
            "producer-candidate.bin", rendered_bytes,
            kind="producer_candidate", revision=f"candidate:{candidate_id}",
        ),
        _artifact(
            "shipped-suffix.bin", shipped_bytes,
            kind="shipped_suffix", revision=f"seal:{delivery_seal}",
        ),
    ]
    for source in candidate.edited_sources:
        artifact_rows.append((source.artifact_id, source.raw_bytes))
        refs.append(_artifact(
            source.artifact_id,
            source.raw_bytes,
            kind="edited_source",
            revision=source.revision,
        ))
    refs_tuple = tuple(sorted(refs))
    by_id = {ref.artifact_id: ref for ref in refs_tuple}
    try:
        current = json.loads(candidate.current_result_bytes)
    except (TypeError, ValueError, json.JSONDecodeError):
        current = None
    source_paths = {source.path for source in candidate.edited_sources}
    complete = (
        candidate.attribution.attributed
        and candidate.attribution.current_verdict == "fail"
        and isinstance(current, dict)
        and current.get("verdict") == "fail"
        and candidate.current_result_sha256 == _sha(candidate.current_result_bytes)
        and bool(candidate.covering_files)
        and candidate.covering_files == candidate.attribution.covering_files
        and tuple(sorted(current.get("files") or ())) == candidate.covering_files
        and bool(candidate.edited_sources)
        and bool(candidate.attribution.implicated_edited_paths)
        and set(candidate.attribution.implicated_edited_paths) <= source_paths
        and all(
            source.sha256 == _sha(source.raw_bytes)
            and source.bytes_length == len(source.raw_bytes)
            and bool(source.revision)
            for source in candidate.edited_sources
        )
        and candidate.actual_event == EVENT_TEST_RESULT
        and candidate.rendered_sha256_16 == _sha(rendered_bytes)[:16]
        and candidate.rendered_chars == len(producer_block)
        and candidate.rendered_bytes_length == len(rendered_bytes)
        and shipped_bytes in (rendered_bytes, b"\n" + rendered_bytes)
        and delivery_seal == _sha(shipped_bytes)[:16]
        and target.replace("\\", "/") in source_paths
        and candidate_id == lane_delivery_candidate_id(
            "verify.horizon.executed", target, shipped_suffix
        )
    )
    source_proofs = tuple(
        ProofRef("source_revision", by_id[source.artifact_id], "$")
        for source in candidate.edited_sources
    )
    return _final(
        evidence_type="covering_red",
        runtime_producer_id="covering_runner",
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        actual_event=candidate.actual_event,
        refs=refs_tuple,
        artifacts=tuple(artifact_rows),
        complete=complete,
        truth_proofs=(
            ProofRef("producer_attribution", by_id["covering-input.json"], "$.attribution"),
            ProofRef("test_result", by_id["current-result.json"], "$.verdict"),
            ProofRef("producer_render", by_id["producer-candidate.bin"], "$"),
            ProofRef("shipped_identity", by_id["shipped-suffix.bin"], "$"),
        ),
        freshness_proofs=(
            *source_proofs,
            ProofRef("producer_event", by_id["covering-input.json"], "$.actual_event"),
        ),
    )


__all__ = [
    "CoveringCandidateInput",
    "EditedSourceInput",
    "FinalAttestationInputs",
    "build_covering_candidate_input",
    "finalize_covering_attestation",
    "finalize_syntax_attestation",
    "lane_delivery_candidate_id",
]
