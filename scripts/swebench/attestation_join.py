"""Join immutable producer attestations to delivered ledger rows → typed truth.

The FACT ``correct_info`` gate reads ``lifecycle.truth_valid``/``authority_valid``.
The generic runtime-ledger writer's self-attested summaries are NOT source truth
(rejected in commit ``47dacfd0f``; the inference shortcut was reverted in
``ea0eb16c0``), so truth stays UNMEASURED for every fact class. The authority that
CAN populate it already exists: immutable ``ProducerAttestation`` bundles, persisted
at the final sealed winner for four families (``syntax_result``, ``covering_red``,
``caller_contract``, ``signature_delta``) via
:func:`groundtruth.runtime.attestation_store.persist_attestation`.

This module is the JOIN and NOTHING ELSE. It is pure, deterministic, LLM-free, and
never mutates ``task_dir``. It:

1. discovers the persisted store bundles under ``task_dir`` (the sidecar directory
   name is ``producer_attestations`` — see ``_attestation_output_root`` in
   ``artifact_deepswe/gt_mini_patch.py``);
2. re-validates every bundle with :func:`producer_attestation.validate` AND an exact
   canonical-byte / index-key / sha check — a bundle that fails ANY check is skipped
   with a named reason (fail-closed; never a crash, never a silent pass);
3. joins each surviving attestation to a DELIVERED runtime-ledger row ONLY on an
   EXACT ``(candidate_id, delivery_seal)`` == ``(row.candidate_id, row.content_sha256_16)``
   identity — the two writers agree by construction:

   * ``delivery_seal`` == ``sealed.rendered_bytes_hash[:16]``
     (``gt_mini_patch.py`` L12709 / L13207), and
     ``rendered_bytes_hash = sha256(rendered_bytes)`` (``adapters/miniswe.py`` L536);
   * the delivered ledger row's ``content_sha256_16`` == ``sha256(content_bytes)[:16]``
     over the SAME shipped bytes (``gt_mini_patch.py`` L8776-8777);
   * ``candidate_id`` == ``envelope.dedup_key``, stamped as a top-level row key
     (``gt_mini_patch.py`` L8845-8847 + the ``extra`` merge at L8783-8785).

Truth/freshness are 3-valued and fail-closed: ``True`` only when EVERY joined
attestation for a class verdicts PASS; ``False`` when ANY verdicts FAIL; ``None``
when no validated, exactly-joined attestation exists (or a joined attestation is
itself UNMEASURED). An UNJOINED attestation contributes NOTHING.
"""

from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from groundtruth.runtime.attestation_store import (
    STORE_ENTRY_SCHEMA,
    attestation_index_key,
)
from groundtruth.runtime.fact_registry import registration_for
from groundtruth.runtime.producer_attestation import (
    FAIL,
    PASS,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
    canonical_bytes,
    canonical_sha256,
    validate,
)

# The persisted store root directory name (``_attestation_output_root`` in
# gt_mini_patch.py joins ``producer_attestations`` onto ``GT_C_OUT``); the store then
# writes ``<root>/index/<index_key>/{attestation.json,entry.json,artifacts/*}``.
_STORE_ROOT_DIRNAME = "producer_attestations"

# The delivered-outcome sentinel written by the runtime ledger (gt_mini_patch.py
# ``_ProductSignalOutcome.DELIVERED = "delivered"``).
_DELIVERED = "delivered"

# The fact classes that have a producer-attestation factory (lane_attestation +
# gateway_attestation_factory + submit_attestation). Only these may receive joined truth;
# every other class stays UNMEASURED. This is the architectural attested set, NOT a
# benchmark selection:
#   syntax_result   <- edit_check       (finalize_syntax_attestation)
#   covering_red    <- covering_runner  (finalize_covering_attestation, "covering_verdict")
#   caller_contract <- contract_map     (gateway, "caller_break")
#   signature_delta <- patch_delta      (gateway, "signature_mismatch"/"companion_surface")
#   submit_refusal  <- submit_gate      (finalize_submit_refusal_attestation) — the PURE
#                      gate kernel's BLOCK verdict, re-run on its own recorded inputs to
#                      reproduce the refusal (truth+authority measured; freshness honest-dark).
#   recovery        <- ss_coherence_v2  (finalize_recovery_attestation, "coherence_collapse")
#                      — the coherence-collapse steer's blind-overwrite churn count, re-derived
#                      from the producer's own edit-event ledger to reproduce the delivered
#                      "rewritten N times" claim (truth+authority measured; freshness honest-dark).
#   localization    <- v1r_brief        (brief_attestation.finalize_localization_attestation) —
#                      the step-0 brief's registry-routed localization BLOCK, bound to its
#                      per-block delivery seal (compound row block_lineage). Truth PASS iff the
#                      producer graph-verified the candidate at BUILD time (witness_verified);
#                      freshness honest-dark (no runtime graph sub-revision proof).
#   newfile_precedent <- change_surface (finalize_newfile_precedent_attestation) — the
#                      REGISTRATION missing-role precedent, re-derived by re-running
#                      change_surface's own registration derivation on the producer-captured
#                      registry bytes (truth+authority measured; freshness MEASURED via the
#                      captured git commit + blob anchor).
ATTESTED_FACT_CLASSES: tuple[str, ...] = (
    "caller_contract",
    "covering_red",
    "localization",
    "newfile_precedent",
    "recovery",
    "signature_delta",
    "submit_refusal",
    "syntax_result",
)


@dataclass(frozen=True)
class TruthJoin:
    """The exactly-joined typed truth for one fact class.

    ``truth``/``freshness`` are ``None`` (UNMEASURED) unless a validated attestation
    joined a DELIVERED row; ``True`` only when every joined attestation verdicts PASS;
    ``False`` when any FAILs. ``attestation_count`` counts ONLY joined attestations
    (unjoined contribute nothing). ``joined_delivery_row_indices`` are the indices, in
    the passed ledger-row list, of the DELIVERED rows that matched.

    ``authority`` (J2b) is the second leg of the FACT ``correct_info`` gate. It is
    ``True`` ONLY on a truth-PASS join — i.e. every joined attestation validated
    (``producer_attestation.validate`` already proved the registered-producer claim:
    ``registered_producer_id == registration.producer`` and ``producer_matches``),
    the join was EXACT (candidate_id + delivery_seal to a DELIVERED row), AND the
    truth predicates aggregated to PASS (``truth is True``). It is ``None`` otherwise
    and is NEVER ``False``: no producer claims negative authority, so an absent or
    non-PASS join is UNMEASURED, not a refutation (correct-or-quiet).
    """

    truth: bool | None
    authority: bool | None
    freshness: bool | None
    attestation_count: int
    joined_delivery_row_indices: tuple[int, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AttestationLoad:
    """The validated attestations plus every named rejection reason (fail-closed).

    ``attestations`` holds only bundles that passed EVERY check; ``diagnostics`` holds
    one ``<index_key>:<reason>`` line per skipped/malformed bundle so the join can be
    audited without inspecting ``task_dir``.
    """

    attestations: tuple[ProducerAttestation, ...] = ()
    diagnostics: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Pure reconstruction of a ProducerAttestation from its canonical to_dict shape.
# producer_attestation exposes ``to_dict``/``canonical_bytes`` but no reader, so the
# join owns a faithful inverse. Any shape violation raises; the caller skips the
# bundle with a reason (fail-closed).
# --------------------------------------------------------------------------- #
class _ReconstructError(ValueError):
    """A stored attestation JSON did not match the expected canonical shape."""


def _req(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise _ReconstructError(f"missing:{key}")
    return mapping[key]


def _artifact_from_mapping(data: Any) -> ArtifactRef:
    return ArtifactRef(
        kind=_req(data, "kind"),
        artifact_id=_req(data, "artifact_id"),
        sha256=_req(data, "sha256"),
        revision=_req(data, "revision"),
    )


def _proof_from_mapping(data: Any) -> ProofRef:
    return ProofRef(
        proof_type=_req(data, "proof_type"),
        artifact=_artifact_from_mapping(_req(data, "artifact")),
        field_path=_req(data, "field_path"),
    )


def _predicate_from_mapping(data: Any) -> PredicateAttestation:
    proofs = _req(data, "proof_refs")
    if not isinstance(proofs, list):
        raise _ReconstructError("proof_refs:not_list")
    return PredicateAttestation(
        predicate_kind=_req(data, "predicate_kind"),
        predicate_id=_req(data, "predicate_id"),
        subject=_req(data, "subject"),
        expectation=_req(data, "expectation"),
        observation=_req(data, "observation"),
        verdict=_req(data, "verdict"),
        proof_refs=tuple(_proof_from_mapping(p) for p in proofs),
    )


def _predicates(data: Any, key: str) -> tuple[PredicateAttestation, ...]:
    raw = _req(data, key)
    if not isinstance(raw, list):
        raise _ReconstructError(f"{key}:not_list")
    return tuple(_predicate_from_mapping(p) for p in raw)


def _attestation_from_mapping(data: Any) -> ProducerAttestation:
    """Rebuild the frozen dataclass from ``producer_attestation.to_dict`` output."""
    artifacts = _req(data, "source_artifacts")
    if not isinstance(artifacts, list):
        raise _ReconstructError("source_artifacts:not_list")
    decision = _req(data, "decision")
    return ProducerAttestation(
        schema=_req(data, "schema"),
        evidence_type=_req(data, "evidence_type"),
        runtime_producer_id=_req(data, "runtime_producer_id"),
        registered_producer_id=_req(data, "registered_producer_id"),
        candidate_id=_req(data, "candidate_id"),
        delivery_seal=_req(data, "delivery_seal"),
        source_artifacts=tuple(_artifact_from_mapping(a) for a in artifacts),
        truth_predicates=_predicates(data, "truth_predicates"),
        freshness_predicates=_predicates(data, "freshness_predicates"),
        decision=DecisionBinding(
            decision_key=_req(decision, "decision_key"),
            open_event=_req(decision, "open_event"),
            required_event=_req(decision, "required_event"),
        ),
    )


# --------------------------------------------------------------------------- #
# Discovery + validation.
# --------------------------------------------------------------------------- #
def _iter_bundle_entries(task_dir: str) -> list[str]:
    """Every ``entry.json`` under a ``producer_attestations/index/<key>/`` bundle.

    Sorted for deterministic diagnostics. Never mutates ``task_dir``.
    """
    pattern = os.path.join(
        task_dir, "**", _STORE_ROOT_DIRNAME, "index", "*", "entry.json"
    )
    return sorted(glob.glob(pattern, recursive=True))


def _load_and_validate_bundle(entry_path: str) -> tuple[ProducerAttestation | None, str]:
    """Return ``(attestation, "")`` for a bundle that passes EVERY check, else
    ``(None, "<index_key>:<reason>")``. Fail-closed: any I/O or shape fault is a
    named skip, never a raise."""
    bundle = os.path.dirname(entry_path)
    key = os.path.basename(bundle)

    def skip(reason: str) -> tuple[None, str]:
        return None, f"{key}:{reason}"

    try:
        raw_entry = _read_bytes(entry_path)
    except OSError:
        return skip("entry_unreadable")
    try:
        entry = json.loads(raw_entry)
    except (ValueError, UnicodeDecodeError):
        return skip("entry_not_json")
    if not isinstance(entry, dict):
        return skip("entry_not_object")
    if entry.get("schema") != STORE_ENTRY_SCHEMA:
        return skip("entry_schema_unsupported")

    attestation_path = os.path.join(bundle, "attestation.json")
    try:
        raw_attestation = _read_bytes(attestation_path)
    except OSError:
        return skip("attestation_unreadable")
    try:
        data = json.loads(raw_attestation)
    except (ValueError, UnicodeDecodeError):
        return skip("attestation_not_json")

    try:
        attestation = _attestation_from_mapping(data)
    except _ReconstructError as exc:
        return skip(f"attestation_shape:{exc}")

    errors = validate(attestation)
    if errors:
        return skip(f"invalid:{errors[0]}")

    # EXACT canonical-byte integrity: the store wrote ``canonical_bytes(attestation)``,
    # so a faithful reconstruction re-serializes byte-identically. Any divergence means
    # the file was tampered or is non-canonical → reject (the fabrication class).
    if canonical_bytes(attestation) != raw_attestation:
        return skip("attestation_noncanonical_or_tampered")

    digest = canonical_sha256(attestation)
    if entry.get("attestation_sha256") != digest:
        return skip("entry_sha_mismatch")
    expected_key = attestation_index_key(
        attestation.candidate_id, attestation.delivery_seal
    )
    if entry.get("index_key") != expected_key or key != expected_key:
        return skip("index_key_mismatch")
    if (
        entry.get("candidate_id") != attestation.candidate_id
        or entry.get("delivery_seal") != attestation.delivery_seal
    ):
        return skip("entry_identity_mismatch")
    return attestation, ""


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def load_attestations(task_dir: str) -> AttestationLoad:
    """Discover, parse, and re-validate every persisted producer-attestation bundle.

    Returns validated attestations plus a named-reason diagnostics list. A malformed,
    invalid, tampered, or identity-mismatched bundle is SKIPPED with a reason — never a
    crash, never a silent pass. Pure and read-only.
    """
    if not isinstance(task_dir, str) or not task_dir:
        return AttestationLoad()
    attestations: list[ProducerAttestation] = []
    diagnostics: list[str] = []
    for entry_path in _iter_bundle_entries(task_dir):
        attestation, reason = _load_and_validate_bundle(entry_path)
        if attestation is None:
            diagnostics.append(reason)
        else:
            attestations.append(attestation)
    return AttestationLoad(tuple(attestations), tuple(diagnostics))


# --------------------------------------------------------------------------- #
# The join.
# --------------------------------------------------------------------------- #
def _index_block_lineage(row: dict, position: int, index: dict[tuple[str, str], list[int]]) -> None:
    """Expose a COMPOUND delivered row's per-block seals to the join.

    The task-start brief is one DELIVERED ledger row with no top-level
    ``(candidate_id, content_sha256_16)`` — its fact-bearing blocks are sealed
    individually under ``block_lineage`` (``gt_headless_runner._brief_delivery_extra``:
    each block carries its own ``candidate_id`` and ``content_sha256_16`` over the exact
    delivered block bytes). Index each such block's identity → the compound row position
    so a producer attestation sealed to a block (e.g. a ``localization`` brief block) can
    join it on the SAME exact identity the native lane rows use. Purely additive: a
    non-compound row (no ``block_lineage``) is unaffected.
    """
    blocks = row.get("block_lineage")
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        candidate_id = block.get("candidate_id")
        seal = block.get("content_sha256_16")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        if not isinstance(seal, str) or not seal:
            continue
        index[(candidate_id, seal)].append(position)


def _delivered_row_index(ledger_rows: Iterable[Any]) -> dict[tuple[str, str], list[int]]:
    """Map ``(candidate_id, content_sha256_16)`` → row indices, DELIVERED rows only.

    Indexes both a row's TOP-LEVEL seal identity (native lane rows) and, for a compound
    delivery (the step-0 brief), every per-block seal under ``block_lineage`` — so a
    brief-block attestation joins on the exact same identity contract.
    """
    index: dict[tuple[str, str], list[int]] = defaultdict(list)
    for position, row in enumerate(ledger_rows or []):
        if not isinstance(row, dict):
            continue
        outcome = row.get("outcome")
        if not isinstance(outcome, str) or outcome.lower() != _DELIVERED:
            continue
        _index_block_lineage(row, position, index)
        candidate_id = row.get("candidate_id")
        seal = row.get("content_sha256_16")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        if not isinstance(seal, str) or not seal:
            continue
        index[(candidate_id, seal)].append(position)
    return index


def _aggregate_bool(verdicts: list[str]) -> bool | None:
    """Fail-closed 3-valued aggregate over per-attestation verdicts.

    FAIL present → ``False``; non-empty and ALL PASS → ``True``; otherwise ``None``
    (an UNMEASURED verdict among them, or empty). Never claims True on partial proof.
    """
    if FAIL in verdicts:
        return False
    if verdicts and all(verdict == PASS for verdict in verdicts):
        return True
    return None


def join_truth(
    attestations: Iterable[ProducerAttestation],
    ledger_rows: Iterable[Any],
) -> dict[str, TruthJoin]:
    """Join validated attestations to DELIVERED ledger rows → per-fact-class truth.

    A join exists ONLY on an EXACT ``(candidate_id, delivery_seal)`` == a DELIVERED
    row's ``(candidate_id, content_sha256_16)`` identity. An attestation that matches no
    delivered row contributes NOTHING (it is not counted, and cannot set truth). The
    returned dict contains ONLY fact classes with at least one joined attestation.
    """
    delivered = _delivered_row_index(ledger_rows)
    joined: dict[str, list[tuple[ProducerAttestation, list[int]]]] = defaultdict(list)
    for attestation in attestations:
        registration = registration_for(attestation.evidence_type)
        if registration is None:
            continue  # validate() already guarantees registration; defensive skip.
        fact_class = registration.fact_class
        row_indices = delivered.get(
            (attestation.candidate_id, attestation.delivery_seal)
        )
        if not row_indices:
            continue  # unjoined → contributes nothing
        joined[fact_class].append((attestation, list(row_indices)))

    result: dict[str, TruthJoin] = {}
    for fact_class, items in joined.items():
        truth_verdicts = [attestation.truth_verdict for attestation, _ in items]
        freshness_verdicts = [attestation.freshness_verdict for attestation, _ in items]
        indices = sorted({index for _, idxs in items for index in idxs})
        reasons = tuple(
            f"joined candidate={attestation.candidate_id!r} seal="
            f"{attestation.delivery_seal} truth={attestation.truth_verdict} "
            f"freshness={attestation.freshness_verdict}"
            for attestation, _ in items
        )
        truth = _aggregate_bool(truth_verdicts)
        # J2b: authority rides ONLY a truth-PASS join. Every item here already
        # validated (load_attestations) and joined EXACTLY (the identity above), so
        # the remaining condition is truth==PASS. Never False — a FAIL/UNMEASURED
        # truth leaves authority UNMEASURED (None), not a negative claim.
        authority = True if truth is True else None
        result[fact_class] = TruthJoin(
            truth=truth,
            authority=authority,
            freshness=_aggregate_bool(freshness_verdicts),
            attestation_count=len(items),
            joined_delivery_row_indices=tuple(indices),
            reasons=reasons,
        )
    return result


def truth_join_to_dict(join: TruthJoin) -> dict[str, Any]:
    """JSON-native projection of a :class:`TruthJoin` for the integrity sidecar."""
    return {
        "truth": join.truth,
        "authority": join.authority,
        "freshness": join.freshness,
        "attestation_count": join.attestation_count,
        "joined_delivery_row_indices": list(join.joined_delivery_row_indices),
        "reasons": list(join.reasons),
    }


__all__ = [
    "ATTESTED_FACT_CLASSES",
    "AttestationLoad",
    "TruthJoin",
    "join_truth",
    "load_attestations",
    "truth_join_to_dict",
]
