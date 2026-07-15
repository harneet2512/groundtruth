from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from groundtruth.runtime.producer_attestation import (
    ATTESTATION_SCHEMA,
    FAIL,
    FRESHNESS,
    PASS,
    TRUTH,
    UNMEASURED,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
    canonical_bytes,
    canonical_sha256,
    to_dict,
    validate,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SEAL = "c" * 16


def _artifact(
    artifact_id: str = "syntax-diagnostic",
    *,
    kind: str = "diagnostic",
    sha256: str = SHA_A,
    revision: str = "patch:42",
) -> ArtifactRef:
    return ArtifactRef(
        kind=kind,
        artifact_id=artifact_id,
        sha256=sha256,
        revision=revision,
    )


def _proof(
    proof_type: str = "producer_observation",
    artifact: ArtifactRef | None = None,
    field_path: str = "diagnostics[0]",
) -> ProofRef:
    return ProofRef(
        proof_type=proof_type,
        artifact=artifact or _artifact(),
        field_path=field_path,
    )


def _predicate(
    predicate_kind: str,
    *,
    predicate_id: str,
    verdict: str = PASS,
    proof_refs: tuple[ProofRef, ...] | None = None,
) -> PredicateAttestation:
    return PredicateAttestation(
        predicate_kind=predicate_kind,
        predicate_id=predicate_id,
        subject="src/core.py@patch:42",
        expectation="producer result describes the exact candidate revision",
        observation="normalized diagnostic and source revision agree",
        verdict=verdict,
        proof_refs=proof_refs if proof_refs is not None else (_proof(),),
    )


def _valid() -> ProducerAttestation:
    source = _artifact()
    return ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type="syntax_result",
        runtime_producer_id="edit_check",
        registered_producer_id="edit_check",
        candidate_id="edit:src/core.py:patch:42",
        delivery_seal=SEAL,
        source_artifacts=(source,),
        truth_predicates=(
            _predicate(TRUTH, predicate_id="diagnostic_matches_candidate"),
        ),
        freshness_predicates=(
            _predicate(FRESHNESS, predicate_id="candidate_revision_current"),
        ),
        decision=DecisionBinding(
            decision_key="is the edit acceptable",
            open_event="edit_result",
            required_event="edit_result",
        ),
    )


def test_valid_registered_producer_attestation_is_immutable_and_clean() -> None:
    attestation = _valid()

    assert validate(attestation) == ()
    assert attestation.truth_verdict == PASS
    assert attestation.freshness_verdict == PASS
    with pytest.raises(dataclasses.FrozenInstanceError):
        attestation.candidate_id = "changed"  # type: ignore[misc]


def test_rejects_unknown_or_non_authoritative_producer_and_registry_claim() -> None:
    unknown = dataclasses.replace(_valid(), evidence_type="invented_fact")
    wrong_owner = dataclasses.replace(_valid(), runtime_producer_id="generic_auditor")
    wrong_registration = dataclasses.replace(
        _valid(), registered_producer_id="covering_runner"
    )

    assert "evidence_type:unregistered:'invented_fact'" in validate(unknown)
    assert "producer:unauthorized:'generic_auditor'" in validate(wrong_owner)
    assert (
        "registered_producer_id:mismatch:'covering_runner'!='edit_check'"
        in validate(wrong_registration)
    )


def test_rejects_unbound_identity_artifact_and_decision_fields() -> None:
    bad = dataclasses.replace(
        _valid(),
        candidate_id="",
        delivery_seal="C" * 16,
        source_artifacts=(),
        decision=DecisionBinding(
            decision_key="some other decision",
            open_event="test_result",
            required_event="test_result",
        ),
    )

    errors = validate(bad)
    assert "candidate_id:empty" in errors
    assert "delivery_seal:not_16_lower_hex" in errors
    assert "source_artifacts:empty" in errors
    assert "decision:key_mismatch" in errors
    assert "decision:required_event_mismatch" in errors


@pytest.mark.parametrize("verdict", [True, "TRUE", "CURRENT", "unknown", ""])
def test_no_generic_boolean_or_untyped_verdict_can_enter_predicates(verdict: object) -> None:
    bad_predicate = dataclasses.replace(
        _valid().truth_predicates[0], verdict=verdict  # type: ignore[arg-type]
    )
    bad = dataclasses.replace(_valid(), truth_predicates=(bad_predicate,))

    assert "truth_predicates[0]:verdict:unknown" in validate(bad)


def test_pass_and_fail_require_exact_proof_while_unmeasured_forbids_fake_proof() -> None:
    for verdict in (PASS, FAIL):
        predicate = _predicate(
            TRUTH, predicate_id="missing-proof", verdict=verdict, proof_refs=()
        )
        errors = validate(dataclasses.replace(_valid(), truth_predicates=(predicate,)))
        assert "truth_predicates[0]:proof_refs:required" in errors

    unmeasured = _predicate(
        TRUTH,
        predicate_id="not-observed",
        verdict=UNMEASURED,
        proof_refs=(_proof(),),
    )
    errors = validate(dataclasses.replace(_valid(), truth_predicates=(unmeasured,)))
    assert "truth_predicates[0]:proof_refs:forbidden_when_unmeasured" in errors


def test_rejects_predicate_kind_swap_and_proof_outside_source_artifacts() -> None:
    foreign_artifact = _artifact("foreign", sha256=SHA_B)
    wrong_kind = _predicate(FRESHNESS, predicate_id="truth-slot")
    foreign_proof = dataclasses.replace(
        _valid().truth_predicates[0], proof_refs=(_proof(artifact=foreign_artifact),)
    )

    errors = validate(
        dataclasses.replace(
            _valid(), truth_predicates=(wrong_kind, foreign_proof)
        )
    )
    assert "truth_predicates[0]:predicate_kind:mismatch" in errors
    assert "truth_predicates[1]:proof_refs[0]:artifact_not_in_sources" in errors


def test_rejects_malformed_or_nondeterministically_ordered_exact_refs() -> None:
    first = _artifact("z-last", sha256=SHA_B)
    second = _artifact("a-first", sha256=SHA_A)
    bad = dataclasses.replace(_valid(), source_artifacts=(first, second, second))

    errors = validate(bad)
    assert "source_artifacts:not_unique_sorted" in errors

    malformed = dataclasses.replace(second, sha256="A" * 64, revision="")
    errors = validate(dataclasses.replace(_valid(), source_artifacts=(malformed,)))
    assert "source_artifacts[0]:sha256:not_64_lower_hex" in errors
    assert "source_artifacts[0]:revision:empty" in errors


def test_canonical_bytes_and_hash_are_exact_stable_and_json_native() -> None:
    attestation = _valid()
    payload = to_dict(attestation)
    expected = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    assert canonical_bytes(attestation) == expected
    assert canonical_bytes(attestation) == canonical_bytes(_valid())
    assert canonical_sha256(attestation) == hashlib.sha256(expected).hexdigest()
    assert payload["schema"] == "gt.producer_attestation.v1"
    assert "truth_valid" not in payload
    assert "freshness_valid" not in payload


def test_candidate_seal_and_revision_are_hash_identity_inputs() -> None:
    base = _valid()
    changed_candidate = dataclasses.replace(base, candidate_id="edit:other")
    changed_seal = dataclasses.replace(base, delivery_seal="d" * 16)
    changed_revision = dataclasses.replace(
        base,
        source_artifacts=(dataclasses.replace(base.source_artifacts[0], revision="patch:43"),),
    )

    assert canonical_sha256(base) != canonical_sha256(changed_candidate)
    assert canonical_sha256(base) != canonical_sha256(changed_seal)
    assert canonical_sha256(base) != canonical_sha256(changed_revision)
