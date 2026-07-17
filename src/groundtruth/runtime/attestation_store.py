"""Atomic persistence for already-authoritative producer attestations.

This module validates identities and bytes only.  It never derives truth,
freshness, timing, delivery, or SS-LIVE status.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .producer_attestation import (
    ArtifactRef,
    ProducerAttestation,
    canonical_bytes,
    canonical_sha256,
    validate,
)

STORE_ENTRY_SCHEMA = "gt.producer_attestation_store_entry.v1"
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AttestationStoreError(ValueError):
    """An input or atomic-persistence contract violation."""


@dataclass(frozen=True)
class StoredAttestation:
    index_key: str
    bundle_dir: Path
    entry_bytes: bytes
    attestation_sha256: str


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def attestation_index_key(candidate_id: str, delivery_seal: str) -> str:
    """Stable full-width key for the candidate-and-final-seal identity."""
    identity = _canonical_json_bytes(
        {"candidate_id": candidate_id, "delivery_seal": delivery_seal}
    )
    return hashlib.sha256(identity).hexdigest()


def _path_safe(artifact_id: object) -> bool:
    return (
        isinstance(artifact_id, str)
        and bool(_SAFE_ARTIFACT_ID.fullmatch(artifact_id))
        and artifact_id not in {".", ".."}
    )


def _validated_inputs(
    attestation: ProducerAttestation,
    artifacts: Mapping[str, bytes],
) -> tuple[tuple[ArtifactRef, ...], dict[str, bytes]]:
    if not isinstance(attestation, ProducerAttestation):
        raise AttestationStoreError("attestation:invalid:wrong_type")
    # Path identities are a stricter store contract than the pure attestation
    # schema, so reject them before creating any directory.
    for ref in attestation.source_artifacts:
        if not _path_safe(getattr(ref, "artifact_id", None)):
            raise AttestationStoreError("artifact_id:path_unsafe")
    errors = validate(attestation)
    if errors:
        raise AttestationStoreError("attestation:invalid:" + "|".join(errors))
    if not isinstance(artifacts, Mapping):
        raise AttestationStoreError("artifacts:not_mapping")

    provided: dict[str, bytes] = {}
    for artifact_id, data in artifacts.items():
        if not _path_safe(artifact_id):
            raise AttestationStoreError("artifact_id:path_unsafe")
        if not isinstance(data, bytes):
            raise AttestationStoreError(f"artifacts:not_bytes:{artifact_id}")
        provided[artifact_id] = data

    refs_by_id: dict[str, ArtifactRef] = {}
    for ref in attestation.source_artifacts:
        if ref.artifact_id in refs_by_id:
            raise AttestationStoreError(f"artifacts:duplicate_id:{ref.artifact_id}")
        refs_by_id[ref.artifact_id] = ref
    expected_ids = set(refs_by_id)
    provided_ids = set(provided)
    missing = sorted(expected_ids - provided_ids)
    extra = sorted(provided_ids - expected_ids)
    if missing:
        raise AttestationStoreError(f"artifacts:missing:{missing[0]}")
    if extra:
        raise AttestationStoreError(f"artifacts:extra:{extra[0]}")
    for artifact_id in sorted(expected_ids):
        ref = refs_by_id[artifact_id]
        digest = hashlib.sha256(provided[artifact_id]).hexdigest()
        if digest != ref.sha256:
            raise AttestationStoreError(f"artifacts:sha256_mismatch:{artifact_id}")
    return attestation.source_artifacts, provided


def _entry_bytes(
    attestation: ProducerAttestation,
    refs: tuple[ArtifactRef, ...],
    index_key: str,
) -> bytes:
    return _canonical_json_bytes(
        {
            "schema": STORE_ENTRY_SCHEMA,
            "index_key": index_key,
            "candidate_id": attestation.candidate_id,
            "delivery_seal": attestation.delivery_seal,
            "attestation_path": "attestation.json",
            "attestation_sha256": canonical_sha256(attestation),
            "artifacts": [
                {
                    "artifact_id": ref.artifact_id,
                    "kind": ref.kind,
                    "revision": ref.revision,
                    "sha256": ref.sha256,
                    "path": f"artifacts/{ref.artifact_id}",
                }
                for ref in refs
            ],
        }
    )


def _write_exact(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _bundle_matches(
    bundle: Path,
    attestation_bytes: bytes,
    entry_bytes: bytes,
    artifacts: Mapping[str, bytes],
) -> bool:
    try:
        if any(path.is_symlink() for path in bundle.rglob("*")):
            return False
        expected_paths = {"attestation.json", "entry.json"} | {
            f"artifacts/{artifact_id}" for artifact_id in artifacts
        }
        actual_paths = {
            path.relative_to(bundle).as_posix()
            for path in bundle.rglob("*")
            if path.is_file()
        }
        if actual_paths != expected_paths:
            return False
        if (bundle / "attestation.json").read_bytes() != attestation_bytes:
            return False
        if (bundle / "entry.json").read_bytes() != entry_bytes:
            return False
        return all(
            (bundle / "artifacts" / artifact_id).read_bytes() == data
            for artifact_id, data in artifacts.items()
        )
    except OSError:
        return False


def persist_attestation(
    attestation: ProducerAttestation,
    artifacts: Mapping[str, bytes],
    output_root: str | os.PathLike[str],
) -> StoredAttestation:
    """Validate and atomically publish one complete immutable bundle."""
    refs, exact_artifacts = _validated_inputs(attestation, artifacts)
    key = attestation_index_key(attestation.candidate_id, attestation.delivery_seal)
    attestation_payload = canonical_bytes(attestation)
    entry_payload = _entry_bytes(attestation, refs, key)
    index_root = Path(output_root) / "index"
    final = index_root / key

    if final.exists():
        if _bundle_matches(final, attestation_payload, entry_payload, exact_artifacts):
            return StoredAttestation(key, final, entry_payload, canonical_sha256(attestation))
        raise AttestationStoreError("persist:existing_conflict")

    index_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=index_root))
    # mkdtemp creates 0o700 (owner-only) by POSIX security contract, and os.replace
    # below preserves that mode onto the published <key>/ bundle. The store is
    # audit-only bytes harvested across a container-root -> host-non-root uid
    # boundary (find/cp as the runner user), so an owner-only bundle dir yields a
    # silent EACCES and the harvest counts 0 despite every bundle being present
    # (live witness: run 29594276655, 15/15 tasks "harvested 0 bundle(s)").
    # Publish world-traversable like every other dir this store creates.
    os.chmod(temporary, 0o755)
    try:
        artifacts_dir = temporary / "artifacts"
        artifacts_dir.mkdir()
        _write_exact(temporary / "attestation.json", attestation_payload)
        _write_exact(temporary / "entry.json", entry_payload)
        for artifact_id in sorted(exact_artifacts):
            _write_exact(artifacts_dir / artifact_id, exact_artifacts[artifact_id])
        try:
            os.replace(temporary, final)
        except OSError as exc:
            if final.exists() and _bundle_matches(
                final, attestation_payload, entry_payload, exact_artifacts
            ):
                return StoredAttestation(
                    key, final, entry_payload, canonical_sha256(attestation)
                )
            raise AttestationStoreError("persist:atomic_publish_failed") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return StoredAttestation(key, final, entry_payload, canonical_sha256(attestation))


__all__ = [
    "AttestationStoreError",
    "STORE_ENTRY_SCHEMA",
    "StoredAttestation",
    "attestation_index_key",
    "persist_attestation",
]
