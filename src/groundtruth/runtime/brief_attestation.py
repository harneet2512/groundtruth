"""Pure final-attestation factory for the step-0 brief's registry-routed blocks.

The task-start brief is *compound* evidence: one delivery whose fact-bearing blocks
are each sealed at BLOCK level (``content_sha256_16`` per block, computed over the
exact block spans in the sealed producer receipt). The
localization blocks are registry-routed to the ``v1r_brief`` producer of the
``localization`` §1 class, so their per-block lineage already carries a
``producer_registration_match``.  What was missing is the *attestation* leg: an
immutable :class:`~groundtruth.runtime.producer_attestation.ProducerAttestation`
bundle bound to a block's exact delivery seal, so that
:mod:`attestation_join` can populate ``lifecycle.truth_valid``/``authority_valid``
for the ``localization`` class exactly as it already does for the native lane classes
(``syntax_result``/``covering_red``/…).

This module is the localization counterpart of :mod:`groundtruth.runtime.lane_attestation`:
a PURE factory that binds the producer's BUILD-TIME graph re-verification of one ranked
candidate to that candidate's delivered block seal.  It never renders text, reads a
repository, persists a file, or changes a model-facing byte — the persist step
(:func:`groundtruth.runtime.attestation_store.persist_attestation`) is the caller's job,
and an incomplete/unverified candidate yields an honestly UNMEASURED truth verdict.

The build-time truth basis is the producer's own generation record:
``witness_verified`` is ``True`` iff the graph localizer produced a VERIFIED graph
witness for the candidate (``relevance_grade == "VERIFIED"`` — a resolved CALLS/IMPORTS
edge/node against ``graph.db`` at build time; ``v1r_brief.py`` L5642).  A candidate the
producer could not verify against the graph is attested UNMEASURED, never PASS.

PURE · DETERMINISTIC · LLM-FREE · stdlib-only · no I/O. Two independent builds from the
same inputs produce byte-identical :func:`~groundtruth.runtime.producer_attestation.canonical_bytes`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .fact_registry import registration_for, required_event
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

# The one localization block class this factory attests. Kept explicit (not inferred
# from a label) so a new brief block class never silently borrows this truth path.
_LOCALIZATION_EVIDENCE_TYPE = "localization"
_LOCALIZATION_RUNTIME_PRODUCER = "v1r_brief"
# The task-start obligations block class. Its registered producer is ``spec`` (the issue
# obligations extractor); its decision boundary is the initial plan at ``task_start``.
_OBLIGATIONS_EVIDENCE_TYPE = "obligations"
_OBLIGATIONS_RUNTIME_PRODUCER = "spec"
# The chronological event at which the "which file to open" decision opens: the agent
# reads the step-0 brief at task_start. (deliver_by / required_event stays search_result.)
_OPEN_EVENT = "task_start"

_SEAL_RE = re.compile(r"^[0-9a-f]{16}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


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


@dataclass(frozen=True)
class FinalAttestationInputs:
    """A built attestation plus the exact artifact bytes to persist with it.

    Mirrors :class:`groundtruth.runtime.lane_attestation.FinalAttestationInputs` so the
    same persist call site (``persist_attestation(final.attestation,
    final.artifact_mapping(), root)``) works unchanged.
    """

    attestation: ProducerAttestation
    artifacts: tuple[tuple[str, bytes], ...]

    def artifact_mapping(self) -> dict[str, bytes]:
        return dict(self.artifacts)


def _predicate(
    kind: str,
    predicate_id: str,
    complete: bool,
    proof_refs: tuple[ProofRef, ...],
    observation: str,
    *,
    subject: str = "step-0 brief localization candidate",
    expectation: str = (
        "producer re-verified the candidate node against graph.db at build time "
        "and it maps 1:1 to the delivered brief block seal"
    ),
) -> PredicateAttestation:
    return PredicateAttestation(
        predicate_kind=kind,
        predicate_id=predicate_id,
        subject=subject,
        expectation=expectation,
        observation=observation if complete else "",
        verdict=PASS if complete else UNMEASURED,
        proof_refs=proof_refs if complete else (),
    )


# --------------------------------------------------------------------------- #
# Producer snapshots — carrying build-time facts to the delivery site.
# --------------------------------------------------------------------------- #
# The factories need facts only the PRODUCER has (the ranked path/rank/witness; the issue
# identity, count and digest), but they can only be finalized at DELIVERY, where the seal
# exists. This is the same carrier `gateway._stash_newfile_precedent_snapshot` uses for
# change_surface: a bounded dict, popped once, that can never raise into the delivery path.
#
# KEYED BY THE ENVELOPE ``dedup_key``, NOT the brief's own candidate id. The brief ids
# ("obl-1", "file-entry-1") are producer-local; the delivery row and the attestation join both
# use the dedup key, which does not exist until ``EvidenceEnvelope.build`` has run. Keying this
# by the brief id would build a stash nothing ever pops — a silent no-op that looks implemented.
_BRIEF_SNAPSHOTS: dict[str, dict[str, Any]] = {}
_BRIEF_SNAPSHOT_CAP = 256


def stash_brief_snapshot(candidate_id: str, snapshot: "dict[str, Any] | None") -> None:
    """Record one producer snapshot under the DELIVERY identity. Never raises.

    A falsy key or snapshot is a no-op rather than a stored ``None``: an empty entry would later
    read as "present" and attest from nothing, which is worse than no attestation at all.
    """
    if not candidate_id or not snapshot:
        return
    try:
        if len(_BRIEF_SNAPSHOTS) >= _BRIEF_SNAPSHOT_CAP:
            # Drop the OLDEST: the deliveries still in flight are the recent ones.
            _BRIEF_SNAPSHOTS.pop(next(iter(_BRIEF_SNAPSHOTS)), None)
        _BRIEF_SNAPSHOTS[candidate_id] = snapshot
    except Exception:  # noqa: BLE001 — audit plumbing can never break a delivery
        return


def pop_brief_snapshot(candidate_id: str) -> "dict[str, Any] | None":
    """Consume the snapshot for a delivered candidate id, or ``None``.

    POP, not peek: one delivery consumes its snapshot, so a later delivery cannot re-attest
    stale producer facts against different bytes.
    """
    if not candidate_id:
        return None
    try:
        return _BRIEF_SNAPSHOTS.pop(candidate_id, None)
    except Exception:  # noqa: BLE001
        return None


def finalize_localization_attestation(
    *,
    candidate_id: str,
    delivery_seal: str,
    block_content_sha256: str,
    delivered_bytes_sha256: str = "",
    path: str,
    rank: int,
    witness: str,
    witness_verified: bool,
    graph_revision: str = "",
) -> "FinalAttestationInputs | None":
    """Bind the producer's build-time graph verification of one ranked candidate to its
    delivered brief-block seal.

    ``delivered_bytes_sha256`` is the digest of the bytes the MODEL RECEIVED, and it is what
    the seal must match. On the step-0 capsule path those bytes are the re-rendered capsule, NOT
    the brief block, so the two digests differ; ``block_content_sha256`` remains producer
    provenance (which block the candidate was rendered into) and is still sealed into the witness
    record. Omitted, it falls back to the block digest — the pre-capsule truth, where the
    delivered bytes ARE the block and one value honestly serves both roles.

    Returns ``None`` when a SOUND binding cannot be formed (a delivery seal that is not
    16 lower-hex, an empty candidate id, or a ``delivered_bytes_sha256`` whose 16-hex
    prefix is not the delivery seal) — there is no attestation to persist, and the class
    stays UNMEASURED via the absent join (fail-closed).

    When the binding is sound the attestation is always built; its TRUTH verdict is PASS
    only when the candidate was graph-verified at build time (``witness_verified`` with a
    non-empty witness, a non-empty path, and a positive rank) and UNMEASURED otherwise.
    FRESHNESS is always UNMEASURED here: the delivered block seal proves *what* was
    delivered, but this factory holds no runtime graph sub-revision proof, so freshness
    stays honestly dark (the ``submit_refusal`` freshness precedent).
    """
    seal = str(delivery_seal or "")
    full = str(block_content_sha256 or "")
    cid = str(candidate_id or "")
    # The SEAL follows the bytes the model actually received. Since the step-0 evidence ships
    # inside a re-rendered CAPSULE, those are no longer the brief block: binding the seal to the
    # block digest would make the bundle assert a falsehood, and a reader reproducing the
    # preimage would hash the block, miss, and correctly call it forged. `block_content_sha256`
    # stays what its name says -- producer provenance, still sealed into the witness record.
    # Omitted -> fall back to the block digest, which IS the delivered byte string on the
    # pre-capsule lanes; one value honestly serves both roles there.
    delivered = str(delivered_bytes_sha256 or "") or full
    if (
        _SEAL_RE.match(seal) is None
        or _FULL_SHA_RE.match(full) is None
        or _FULL_SHA_RE.match(delivered) is None
        or delivered[:16] != seal
        or not cid.strip()
    ):
        return None

    registration = registration_for(_LOCALIZATION_EVIDENCE_TYPE)
    if registration is None:  # defensive — localization is a registered §1 class
        return None

    path_str = str(path or "")
    witness_str = str(witness or "")
    rank_ok = isinstance(rank, int) and not isinstance(rank, bool) and rank > 0
    truth_complete = bool(
        witness_verified is True
        and path_str.strip()
        and witness_str.strip()
        and rank_ok
    )

    # The producer's own build-time verification record — a SMALL canonical artifact
    # (never graph.db); its sha256 seals exactly the fields the truth predicate proves.
    witness_record: dict[str, Any] = {
        "candidate_id": cid,
        "path": path_str,
        "rank": int(rank) if rank_ok else 0,
        "witness": witness_str,
        "witness_verified": bool(witness_verified),
        "block_content_sha256": full,
    }
    witness_bytes = _canonical(witness_record)
    artifact_id = "localization-witness.json"
    ref = ArtifactRef(
        kind="localization_witness",
        artifact_id=artifact_id,
        sha256=_sha(witness_bytes),
        revision=f"graph:{graph_revision or 'unversioned'}",
    )
    truth_proofs = tuple(sorted((
        ProofRef("producer_graph_verification", ref, "$.witness_verified"),
        ProofRef("candidate_path", ref, "$.path"),
        ProofRef("rank_derivation", ref, "$.rank"),
    )))

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=_LOCALIZATION_EVIDENCE_TYPE,
        runtime_producer_id=_LOCALIZATION_RUNTIME_PRODUCER,
        registered_producer_id=registration.producer,
        candidate_id=cid,
        delivery_seal=seal,
        source_artifacts=(ref,),
        truth_predicates=(
            _predicate(
                TRUTH,
                "localization:truth",
                truth_complete,
                truth_proofs,
                "graph-verified candidate joins the delivered block seal",
            ),
        ),
        freshness_predicates=(
            _predicate(FRESHNESS, "localization:freshness", False, (), ""),
        ),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            open_event=_OPEN_EVENT,
            required_event=required_event(_LOCALIZATION_EVIDENCE_TYPE) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        # A build defect (unregistered/producer mismatch) must fail closed, not ship a
        # malformed bundle. The caller treats None as "no attestation" (correct-or-quiet).
        return None
    return FinalAttestationInputs(attestation, ((artifact_id, witness_bytes),))


def finalize_obligations_attestation(
    *,
    candidate_id: str,
    delivery_seal: str,
    block_content_sha256: str,
    delivered_bytes_sha256: str = "",
    issue_sha256: str,
    issue_revision: str,
    obligation_count: int,
    obligations_digest: str,
) -> "FinalAttestationInputs | None":
    """Bind the extracted task-start obligations record to its delivered brief-block seal.

    The obligations block is compound evidence: its fact-bearing bytes are sealed per block
    (``block_content_sha256`` over the exact delivered obligations block). This factory binds
    that seal to the producer's build-time obligations record — the EXACT issue/spec source
    identity (``issue_sha256`` + ``issue_revision``) the obligations were extracted from, and
    the digest + count of the extracted obligations (persisted in ``brief_result.json``).

    ``delivered_bytes_sha256`` is the digest of the bytes the MODEL RECEIVED and is what the
    seal must match; on the step-0 capsule path that is the re-rendered capsule, not this block.
    ``block_content_sha256`` stays producer provenance. Omitted, it falls back to the block digest
    (the pre-capsule case, where the delivered bytes ARE the block).

    Returns ``None`` when a SOUND binding cannot be formed (a delivery seal that is not 16
    lower-hex; a ``delivered_bytes_sha256`` whose 16-hex prefix is not the seal; an empty
    candidate id) — the class stays UNMEASURED via the absent join (fail-closed).

    TRUTH is PASS only when the binding is sound AND the producer supplied a real obligations
    record: a valid 64-hex ``issue_sha256``, a non-empty ``issue_revision``, a positive
    ``obligation_count``, and a valid 64-hex ``obligations_digest``. A fabricated record
    (bad/empty issue sha, zero obligations, bad digest) yields an honest UNMEASURED verdict,
    never PASS. FRESHNESS rides the SAME issue binding — the obligations FACT's sole freshness
    dependency is the issue (``fact_registry`` ``freshness_deps=("issue",)``), so a bound issue
    revision is a real, re-verifiable freshness proof.
    """
    seal = str(delivery_seal or "")
    full = str(block_content_sha256 or "")
    cid = str(candidate_id or "")
    # The SEAL follows the bytes the model actually received. Since the step-0 evidence ships
    # inside a re-rendered CAPSULE, those are no longer the brief block: binding the seal to the
    # block digest would make the bundle assert a falsehood, and a reader reproducing the
    # preimage would hash the block, miss, and correctly call it forged. `block_content_sha256`
    # stays what its name says -- producer provenance, still sealed into the witness record.
    # Omitted -> fall back to the block digest, which IS the delivered byte string on the
    # pre-capsule lanes; one value honestly serves both roles there.
    delivered = str(delivered_bytes_sha256 or "") or full
    if (
        _SEAL_RE.match(seal) is None
        or _FULL_SHA_RE.match(full) is None
        or _FULL_SHA_RE.match(delivered) is None
        or delivered[:16] != seal
        or not cid.strip()
    ):
        return None

    registration = registration_for(_OBLIGATIONS_EVIDENCE_TYPE)
    if registration is None:  # defensive — obligations is a registered §1 class
        return None

    issue_sha = str(issue_sha256 or "")
    issue_rev = str(issue_revision or "")
    digest = str(obligations_digest or "")
    count_ok = (
        isinstance(obligation_count, int)
        and not isinstance(obligation_count, bool)
        and obligation_count > 0
    )
    complete = bool(
        _FULL_SHA_RE.match(issue_sha) is not None
        and issue_rev.strip()
        and count_ok
        and _FULL_SHA_RE.match(digest) is not None
    )

    # The producer's own build-time obligations record — a SMALL canonical artifact (never
    # the whole issue); its sha256 seals exactly the fields the truth/freshness predicates
    # prove: the source issue identity and the extracted obligations digest.
    record: dict[str, Any] = {
        "candidate_id": cid,
        "issue_sha256": issue_sha,
        "issue_revision": issue_rev,
        "obligation_count": int(obligation_count) if count_ok else 0,
        "obligations_digest": digest,
        "block_content_sha256": full,
    }
    record_bytes = _canonical(record)
    artifact_id = "obligations-record.json"
    ref = ArtifactRef(
        kind="obligations_record",
        artifact_id=artifact_id,
        sha256=_sha(record_bytes),
        revision=issue_rev or f"issue:{issue_sha or 'unversioned'}",
    )
    truth_proofs = tuple(sorted((
        ProofRef("issue_source_identity", ref, "$.issue_sha256"),
        ProofRef("obligations_digest", ref, "$.obligations_digest"),
        ProofRef("obligation_count", ref, "$.obligation_count"),
    )))
    freshness_proofs = tuple(sorted((
        ProofRef("issue_source_identity", ref, "$.issue_sha256"),
        ProofRef("issue_revision", ref, "$.issue_revision"),
    )))

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=_OBLIGATIONS_EVIDENCE_TYPE,
        runtime_producer_id=_OBLIGATIONS_RUNTIME_PRODUCER,
        registered_producer_id=registration.producer,
        candidate_id=cid,
        delivery_seal=seal,
        source_artifacts=(ref,),
        truth_predicates=(
            _predicate(
                TRUTH,
                "obligations:truth",
                complete,
                truth_proofs,
                "extracted obligations record binds the delivered block seal",
                subject="step-0 brief obligations block",
                expectation=(
                    "producer extracted the obligations from the exact issue source "
                    "and the record maps 1:1 to the delivered brief block seal"
                ),
            ),
        ),
        freshness_predicates=(
            _predicate(
                FRESHNESS,
                "obligations:freshness",
                complete,
                freshness_proofs,
                "obligations bound to the exact issue source revision",
                subject="step-0 brief obligations block",
                expectation=(
                    "the delivered obligations bind the exact issue source revision"
                ),
            ),
        ),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            open_event=_OPEN_EVENT,
            required_event=required_event(_OBLIGATIONS_EVIDENCE_TYPE) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        return None
    return FinalAttestationInputs(attestation, ((artifact_id, record_bytes),))


__all__ = [
    "FinalAttestationInputs",
    "finalize_localization_attestation",
    "finalize_obligations_attestation",
]
