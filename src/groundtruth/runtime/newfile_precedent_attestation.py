"""Producer-owned truth+freshness attestation for the ``newfile_precedent`` FACT class.

The ``change_surface`` engine (``groundtruth.pretask.change_surface``) is a PURE,
deterministic, LLM-free detector: given the issue text + the repo TREE it emits
HYPOTHESIS-tier facts about the change surfaces a new capability is missing.  The one
delivered claim with concrete, re-derivable BYTE evidence is the REGISTRATION
missing-role: *"registry ``<file>`` registers N siblings but not ``<entity>``"* plus the
exact registry code lines the sibling members are registered on.

The prior audit verdict was that this claim is honestly re-verifiable git-history/tree
evidence, but the producer captured NO revision anchor at production time and the offline
grader never opens the repo — so no attestation could exist.  This module closes that:

  * :func:`build_registration_snapshot` runs at PRODUCTION TIME (inside the change_surface
    gateway producer) and captures the producer's OWN inputs for the registration claim:
    the exact registry-file BYTES it read (bounded identically to ``change_surface``'s own
    ``_read_text``), the sibling group MEMBERS, the git BLOB id of those bytes (computed
    purely, no subprocess), and the repo HEAD commit anchor (one bounded ``git rev-parse``).
  * :func:`finalize_newfile_precedent_attestation` is PURE (no I/O).  It proves TRUTH by
    RE-RUNNING ``change_surface``'s OWN registration derivation
    (``_code_lines`` + ``_ref_pattern`` — imported, never re-implemented) on the snapshot's
    captured bytes and confirming it reproduces the delivered registration claim (>=2
    sibling registrations, the entity NOT among them, the exact header line present in the
    shipped bytes).  This is the covering_red epistemic standard: bind the producer's
    recorded inputs and re-derive them deterministically.

FRESHNESS is honestly MEASURED here (unlike ``submit_refusal``): the snapshot genuinely
carries a git COMMIT anchor (``repo_head``) and the registry file's git BLOB id
(``git_blob_sha``), both re-checkable by a later auditor.  Freshness is PASS only when the
commit anchor is a real 40-hex sha AND the blob id is self-consistent with the captured
bytes; a missing commit anchor stays honestly UNMEASURED.

Correct-or-quiet: any incomplete / tampered / mismatched join is UNMEASURED (never a false
PASS).  A tampered snapshot (bytes whose git blob id no longer matches the recorded anchor)
fails the self-seal and is rejected — truth AND freshness UNMEASURED.  The factory changes
no delivered byte; it performs no I/O and reads no flags.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from groundtruth.pretask.change_surface import (
    _MAX_EVIDENCE_LINES,
    _MAX_FILE_BYTES,
    _MIN_SIBLINGS,
    ROLE_REGISTRATION,
    _code_lines,
    _ref_pattern,
)

from .fact_registry import EVENTS as _EVENTS
from .fact_registry import registration_for, required_event
from .lane_attestation import FinalAttestationInputs
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

_SNAPSHOT_SCHEMA = "gt.newfile_precedent_snapshot.v1"
_PRODUCER_ID = "change_surface"
_FACT_CLASS = "newfile_precedent"
_ACTUAL_EVENT = "failed_search"  # the §1 newfile_precedent deliver_by boundary

__all__ = [
    "NewfilePrecedentSnapshot",
    "build_registration_snapshot",
    "finalize_newfile_precedent_attestation",
    "git_blob_sha",
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal16(text: str) -> str:
    """The delivery seal the runtime ledger stamps: truncated sha256 over the exact
    shipped bytes, with the SAME ``surrogatepass`` encoding the ledger writer uses."""
    return _sha(text.encode("utf-8", "surrogatepass"))[:16]


def git_blob_sha(data: bytes) -> str:
    """The git BLOB object id of ``data`` — ``sha1("blob <len>\\0" + data)``.

    Computed purely (no subprocess), so the same value a later auditor gets from
    ``git hash-object`` / ``git rev-parse HEAD:<path>`` on the committed file. This is the
    git-native content anchor that makes the registry-file revision re-checkable.
    """
    header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 — git object id, not crypto


def _is_commit_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class NewfilePrecedentSnapshot:
    """The producer's OWN captured inputs for ONE registration missing-role claim.

    Every field is an input the ``change_surface`` producer actually read (or a pure
    function of one), never a value reconstructed from the delivered output:

      * ``registration_file``  the registry rel-path the claim quotes;
      * ``registration_bytes`` the EXACT bytes ``change_surface`` read from that file
        (bounded identically to its ``_read_text``);
      * ``members``            the sibling group's member tokens (the registration
        derivation runs ``_ref_pattern(member)`` over the file for each);
      * ``entity``             the new capability the issue names (the claim's subject);
      * ``git_blob_sha``       the git blob id of ``registration_bytes`` (self-seal);
      * ``repo_head``          the repo HEAD commit sha (the freshness commit anchor),
        or ``""`` when git was unavailable at capture (freshness then UNMEASURED).
    """

    registration_file: str
    registration_bytes: bytes
    members: tuple[str, ...]
    entity: str
    git_blob_sha: str
    repo_head: str = ""
    role: str = ROLE_REGISTRATION

    def snapshot_meta(self) -> dict[str, object]:
        """The JSON-native snapshot metadata (bytes stored as a separate artifact)."""
        return {
            "schema": _SNAPSHOT_SCHEMA,
            "fact_class": _FACT_CLASS,
            "role": self.role,
            "entity": self.entity,
            "registration_file": self.registration_file,
            "members": list(self.members),
            "registration_bytes_sha256": _sha(self.registration_bytes),
            "git_blob_sha": self.git_blob_sha,
            "repo_head": self.repo_head,
        }


def build_registration_snapshot(
    *,
    entity: str,
    registration_file: str,
    members,
    repo_root: str,
    repo_head: str = "",
) -> "NewfilePrecedentSnapshot | None":
    """Capture the producer-owned snapshot for a registration claim, at production time.

    Reads the registry file's bytes EXACTLY as ``change_surface._read_text`` does (bounded
    by ``_MAX_FILE_BYTES``) and computes the git blob anchor purely.  ``repo_head`` is the
    caller-supplied commit anchor (the one bounded ``git rev-parse HEAD`` the gateway runs;
    ``""`` when unavailable).  Returns ``None`` correct-or-quiet on any unusable input or
    unreadable file — the caller then simply persists no attestation.
    """
    import os

    if not isinstance(entity, str) or not entity.strip():
        return None
    if not isinstance(registration_file, str) or not registration_file.strip():
        return None
    member_tuple = tuple(
        m for m in dict.fromkeys(members or ()) if isinstance(m, str) and m
    )
    if len(member_tuple) < _MIN_SIBLINGS:
        return None
    if not isinstance(repo_root, str) or not repo_root:
        return None
    path = registration_file if os.path.isabs(registration_file) else os.path.join(
        repo_root, registration_file
    )
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES)
    except OSError:
        return None
    if not raw:
        return None
    head = repo_head if _is_commit_sha(repo_head) else ""
    return NewfilePrecedentSnapshot(
        registration_file=registration_file,
        registration_bytes=raw,
        members=member_tuple,
        entity=entity,
        git_blob_sha=git_blob_sha(raw),
        repo_head=head,
    )


def _rederive_registration(
    snapshot: NewfilePrecedentSnapshot,
) -> "tuple[int, list[tuple[int, str]], bool] | None":
    """Re-run change_surface's OWN registration derivation on the snapshot bytes.

    Returns ``(distinct, reg_lines, entity_registered)`` reproducing exactly what
    ``change_surface._detect_registry`` + ``_entity_holes`` compute, or ``None`` when the
    self-seal is broken (tampered bytes) — the caller treats ``None`` as UNMEASURED.

    ``distinct``           number of sibling members with >=1 code-line registration;
    ``reg_lines``          the exact ``[(lineno, line)]`` the delivered claim quotes;
    ``entity_registered``  whether the entity itself is already registered.
    """
    if git_blob_sha(snapshot.registration_bytes) != snapshot.git_blob_sha:
        return None  # tampered snapshot — bytes no longer match their sealed blob id
    text = snapshot.registration_bytes.decode("utf-8", errors="replace")
    code_lines = _code_lines(text)
    refs: dict[str, list[tuple[int, str]]] = {}
    for member in snapshot.members:
        pattern = _ref_pattern(member)
        for lineno, line in code_lines:
            if pattern.search(line):
                refs.setdefault(member, []).append((lineno, line))
    distinct = len(refs)
    entity_pattern = _ref_pattern(snapshot.entity)
    entity_registered = any(
        entity_pattern.search(line)
        for lines in refs.values()
        for _, line in lines
    )
    reg_lines: list[tuple[int, str]] = []
    for member in sorted(refs):
        reg_lines.extend(refs[member][:1])
    reg_lines = sorted(set(reg_lines))[:_MAX_EVIDENCE_LINES]
    return distinct, reg_lines, entity_registered


def finalize_newfile_precedent_attestation(
    snapshot: NewfilePrecedentSnapshot,
    *,
    evidence_type: str,
    delivered_block: str,
    candidate_id: str,
    delivery_seal: str,
    actual_event: str = _ACTUAL_EVENT,
) -> FinalAttestationInputs:
    """Bind the exact delivered registration missing-role to its re-derived claim.

    ``snapshot``        the producer-owned capture (:func:`build_registration_snapshot`).
    ``evidence_type``   the shipped gateway type (``missing_role:registration``).
    ``delivered_block`` the EXACT shipped bytes the model saw (the delivered candidate).
    ``candidate_id``    the ledger row's ``candidate_id`` (the envelope ``dedup_key``).
    ``delivery_seal``   the ledger row's ``content_sha256_16`` over the shipped bytes.

    Truth is PASS only when the snapshot self-seal holds, the re-derivation reproduces a
    genuine registration precedent (>=2 sibling registrations, the entity NOT registered),
    the delivered header line is present in the shipped bytes, and the seal covers those
    bytes.  Freshness is PASS only when the git commit anchor + blob id are captured and
    self-consistent; otherwise UNMEASURED.  Raises only when the constructed attestation
    would be structurally invalid (a malformed seal) — the audit-persistence caller
    swallows that and simply does not persist.
    """
    registration = registration_for(evidence_type)
    if registration is None or registration.fact_class != _FACT_CLASS:
        raise ValueError(f"{evidence_type!r} is not a newfile_precedent evidence type")

    block_bytes = (delivered_block or "").encode("utf-8", "surrogatepass")
    meta = snapshot.snapshot_meta()
    snapshot_meta_bytes = _canonical(meta)

    snapshot_ref = ArtifactRef(
        kind="producer_snapshot",
        artifact_id="snapshot.json",
        sha256=_sha(snapshot_meta_bytes),
        revision=f"blob:{snapshot.git_blob_sha}",
    )
    source_ref = ArtifactRef(
        kind="registration_source",
        artifact_id="registration-source.bin",
        sha256=_sha(snapshot.registration_bytes),
        revision=f"blob:{snapshot.git_blob_sha}",
    )
    rendered_ref = ArtifactRef(
        kind="rendered_candidate",
        artifact_id="rendered-candidate.bin",
        sha256=_sha(block_bytes),
        revision=f"seal:{delivery_seal}",
    )
    refs = tuple(sorted((snapshot_ref, source_ref, rendered_ref)))
    artifacts = (
        ("snapshot.json", snapshot_meta_bytes),
        ("registration-source.bin", snapshot.registration_bytes),
        ("rendered-candidate.bin", block_bytes),
    )

    rederived = _rederive_registration(snapshot)
    reproduced = False
    if rederived is not None and snapshot.role == ROLE_REGISTRATION:
        distinct, _reg_lines, entity_registered = rederived
        header = (
            f"registry {snapshot.registration_file} registers "
            f"{distinct} siblings but not '{snapshot.entity}'"
        )
        reproduced = (
            distinct >= _MIN_SIBLINGS
            and not entity_registered
            and bool(delivered_block)
            and header in delivered_block
        )

    # #40: the boundary is DERIVED from the evidence type, not hardcoded. The postcreate form
    # (`missing_role_postcreate:*`) is the SAME fact at an honest post-creation edit boundary and
    # the registry gives it its own `deliver_by` (edit_result). Comparing it against the
    # pre-create constant would read UNMEASURED for every postcreate claim; defaulting its
    # `open_event` to that constant would assert a boundary the delivery never touched -- the
    # "false 100% ON_TIME" defect. Pre-create is byte-identical: its derived boundary IS
    # `failed_search`.
    _boundary = required_event(evidence_type) or _ACTUAL_EVENT
    truth_complete = bool(
        reproduced
        and isinstance(candidate_id, str)
        and candidate_id
        and isinstance(delivery_seal, str)
        and delivery_seal == _seal16(delivered_block)
        and actual_event == _boundary
    )

    # FRESHNESS is genuinely measured: the snapshot binds the claim to a re-checkable git
    # commit + blob anchor. It rides the same self-seal (a tampered snapshot fails both).
    freshness_complete = bool(
        rederived is not None
        and _is_commit_sha(snapshot.repo_head)
        and snapshot.git_blob_sha == git_blob_sha(snapshot.registration_bytes)
        and truth_complete
    )

    truth_proofs = (
        ProofRef("producer_snapshot", snapshot_ref, "$.members"),
        ProofRef("producer_snapshot", snapshot_ref, "$.registration_file"),
        ProofRef("registration_source", source_ref, "$"),
        ProofRef("shipped_identity", rendered_ref, "$"),
    )
    truth_predicate = PredicateAttestation(
        predicate_kind=TRUTH,
        predicate_id=f"{_FACT_CLASS}:truth",
        subject="exact delivered registration missing-role candidate",
        expectation=(
            "change_surface's own registration derivation, re-run on the captured "
            "registry bytes, reproduces the delivered precedent"
        ),
        observation=(
            "sibling-registration precedent reproduced from the producer's own bytes"
            if truth_complete
            else "registration claim not reproduced from a sealed snapshot"
        ),
        verdict=PASS if truth_complete else UNMEASURED,
        proof_refs=tuple(sorted(truth_proofs)) if truth_complete else (),
    )
    freshness_proofs = (
        ProofRef("commit_anchor", snapshot_ref, "$.repo_head"),
        ProofRef("blob_anchor", snapshot_ref, "$.git_blob_sha"),
    )
    freshness_predicate = PredicateAttestation(
        predicate_kind=FRESHNESS,
        predicate_id=f"{_FACT_CLASS}:freshness",
        subject="exact delivered registration missing-role candidate",
        expectation="a re-checkable git commit + blob anchor binds the registry bytes",
        observation=(
            "registry bytes bound to repo HEAD and their git blob id"
            if freshness_complete
            else ""
        ),
        verdict=PASS if freshness_complete else UNMEASURED,
        proof_refs=tuple(sorted(freshness_proofs)) if freshness_complete else (),
    )

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=evidence_type,
        runtime_producer_id=_PRODUCER_ID,
        registered_producer_id=registration.producer,
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        source_artifacts=refs,
        truth_predicates=(truth_predicate,),
        freshness_predicates=(freshness_predicate,),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            open_event=actual_event if actual_event in _EVENTS else _boundary,
            required_event=required_event(evidence_type) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        raise ValueError("invalid newfile_precedent attestation: " + "|".join(errors))
    return FinalAttestationInputs(attestation, tuple(sorted(artifacts)))
