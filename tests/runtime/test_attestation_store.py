from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from groundtruth.runtime.attestation_store import (
    AttestationStoreError,
    attestation_index_key,
    persist_attestation,
)
from groundtruth.runtime.producer_attestation import (
    ATTESTATION_SCHEMA,
    FRESHNESS,
    PASS,
    TRUTH,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
    canonical_bytes,
)


def _attestation(artifacts: dict[str, bytes]) -> ProducerAttestation:
    refs = tuple(
        sorted(
            ArtifactRef(
                kind="producer_input",
                artifact_id=artifact_id,
                sha256=hashlib.sha256(data).hexdigest(),
                revision="patch:42",
            )
            for artifact_id, data in artifacts.items()
        )
    )
    proof = ProofRef("producer_observation", refs[0], "$.verdict")
    return ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type="syntax_result",
        runtime_producer_id="edit_check",
        registered_producer_id="edit_check",
        candidate_id="edit:src/core.py:patch:42",
        delivery_seal="c" * 16,
        source_artifacts=refs,
        truth_predicates=(PredicateAttestation(
            TRUTH, "truth", "source", "matches", "matched", PASS, (proof,)
        ),),
        freshness_predicates=(PredicateAttestation(
            FRESHNESS, "fresh", "source", "current", "current", PASS, (proof,)
        ),),
        decision=DecisionBinding(
            "is the edit acceptable", "edit_result", "edit_result"
        ),
    )


def test_persists_one_atomic_canonical_bundle_keyed_by_candidate_and_seal(tmp_path):
    artifacts = {"diagnostic.json": b'{"verdict":"syntax_error"}', "source.bin": b"x\x00y"}
    attestation = _attestation(artifacts)

    stored = persist_attestation(attestation, artifacts, tmp_path)

    expected_key = attestation_index_key(attestation.candidate_id, attestation.delivery_seal)
    assert stored.index_key == expected_key
    assert stored.bundle_dir == tmp_path / "index" / expected_key
    assert (stored.bundle_dir / "attestation.json").read_bytes() == canonical_bytes(attestation)
    for artifact_id, data in artifacts.items():
        assert (stored.bundle_dir / "artifacts" / artifact_id).read_bytes() == data
    entry_bytes = (stored.bundle_dir / "entry.json").read_bytes()
    assert entry_bytes == stored.entry_bytes
    entry = json.loads(entry_bytes)
    assert entry["candidate_id"] == attestation.candidate_id
    assert entry["delivery_seal"] == attestation.delivery_seal
    assert entry["index_key"] == expected_key
    assert [row["artifact_id"] for row in entry["artifacts"]] == sorted(artifacts)


def test_same_exact_bundle_is_idempotent(tmp_path):
    artifacts = {"source.bin": b"abc"}
    attestation = _attestation(artifacts)

    first = persist_attestation(attestation, artifacts, tmp_path)
    second = persist_attestation(attestation, dict(artifacts), tmp_path)

    assert first == second
    assert list((tmp_path / "index").iterdir()) == [first.bundle_dir]


@pytest.mark.parametrize(
    "artifact_id",
    ["../escape", "a/b", r"a\b", ".", "..", "C:drive", "", ".hidden"],
)
def test_rejects_path_unsafe_artifact_ids(tmp_path, artifact_id):
    artifacts = {artifact_id: b"abc"}
    attestation = _attestation(artifacts)

    with pytest.raises(AttestationStoreError, match="artifact_id:path_unsafe"):
        persist_attestation(attestation, artifacts, tmp_path)

    assert not (tmp_path / "index").exists()


def test_rejects_missing_extra_and_sha_mismatch_before_writing(tmp_path):
    expected = {"source.bin": b"abc"}
    attestation = _attestation(expected)

    cases = [
        ({}, "artifacts:missing:source.bin"),
        ({"source.bin": b"abc", "extra.bin": b"x"}, "artifacts:extra:extra.bin"),
        ({"source.bin": b"changed"}, "artifacts:sha256_mismatch:source.bin"),
    ]
    for provided, message in cases:
        with pytest.raises(AttestationStoreError, match=message):
            persist_attestation(attestation, provided, tmp_path)
    assert not (tmp_path / "index").exists()


def test_rejects_nonbytes_and_invalid_attestation(tmp_path):
    artifacts = {"source.bin": b"abc"}
    attestation = _attestation(artifacts)
    invalid = dataclasses.replace(attestation, delivery_seal="BAD")

    with pytest.raises(AttestationStoreError, match="attestation:invalid"):
        persist_attestation(invalid, artifacts, tmp_path)
    with pytest.raises(AttestationStoreError, match="artifacts:not_bytes:source.bin"):
        persist_attestation(attestation, {"source.bin": "abc"}, tmp_path)  # type: ignore[dict-item]
    assert not (tmp_path / "index").exists()


def test_failed_atomic_publish_leaves_no_entry_or_temp_directory(tmp_path, monkeypatch):
    artifacts = {"source.bin": b"abc"}
    attestation = _attestation(artifacts)
    import groundtruth.runtime.attestation_store as store

    def fail_replace(*args, **kwargs):
        raise OSError("publish failed")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(AttestationStoreError, match="persist:atomic_publish_failed"):
        persist_attestation(attestation, artifacts, tmp_path)

    index = tmp_path / "index"
    assert not index.exists() or list(index.iterdir()) == []
