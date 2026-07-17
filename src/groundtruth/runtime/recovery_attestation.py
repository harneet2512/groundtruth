"""Producer-owned truth attestation for the recovery FACT class (``coherence_collapse``).

The recovery fact class (registry ``recovery``, target decision "whether to pivot") has a
concrete LIVE producer on the mini seam: the coherence-collapse steer (lane kind
``detect.coherence``, ``evidence_type`` ``coherence_collapse``, runtime producer
``ss_coherence_v2``; ``coherence_collapse`` aliases to ``recovery`` in the fact registry).
Its steer lane is already sealed via ``_seal_lane_delivery`` (gt_mini_patch.py), so the
``(candidate_id, delivery_seal)`` join to a DELIVERED runtime-ledger row is feasible; what was
missing is PRODUCER-OWNED TRUTH.

The coherence steer's FACTUAL CLAIM is "you have rewritten <file> N times with no passing
test between edits", where N is computed PURELY from the seam's OWN edit-event ledger
(``_ss_coherence_churn``: byte-proven successful writes to THAT EXACT path SINCE the last
passing test; SS audit finding: this "rewritten N times" was factually WRONG on ~16/20 fires
before the V2 gate). This factory captures an IMMUTABLE snapshot of that producer-owned
evidence (the successful-write steps + passing-test steps the churn decision consumed) and
proves TRUTH by RE-RUNNING the PURE churn predicate on the snapshot and confirming it (a)
reproduces the same churn count N (>=3) AND (b) that N is exactly the count the DELIVERED
steer CLAIMS. This is the covering_red epistemic standard applied to the recovery steer: the
attestation binds the producer's recorded decision inputs and re-derives the delivered claim
deterministically — it re-runs no test, reads no graph, reads no flags, and changes no
delivered bytes.

Producer-owned + deterministically re-verifiable at production time; correct-or-quiet
(UNMEASURED) on any incomplete or mismatched join.

FRESHNESS is honestly UNMEASURED. The registry names ``episode_state`` as this fact's
freshness dependency, but the snapshot carries action-count STEPS, not a graph/content
revision anchor, so no honest freshness proof exists here. Rather than fabricate one (the
ea0eb16c0 lesson), the freshness predicate is UNMEASURED — truth and authority (the two
``correct_info`` legs) are measured; freshness stays honestly dark (parity with
``submit_refusal``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

from .fact_registry import EVENT_EDIT_RESULT, registration_for, required_event
from .lane_attestation import FinalAttestationInputs, lane_delivery_candidate_id
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

# The recovery-class producer that this factory attests. ``coherence_collapse`` is the
# gateway/seam evidence_type; it resolves to the canonical ``recovery`` registration
# (producer ``governor``) via the registry alias table, and its authorized runtime producer
# is ``ss_coherence_v2`` (fact_registry ``_EVIDENCE_TYPE_PRODUCERS``).
_EVIDENCE_TYPE = "coherence_collapse"
_PRODUCER_ID = "ss_coherence_v2"
# The lane ``kind`` the coherence steer seals under (gt_mini_patch ``_seal_lane_delivery`` uses
# it as the legacy-fallback envelope identity, producer==evidence_type==kind, so the sealed
# ``candidate_id`` == ``lane_delivery_candidate_id(_LANE_KIND, target, shipped)``).
_LANE_KIND = "detect.coherence"
# The coherence steer is produced AND delivered at the edit-result observation (the registry
# per-evidence-type boundary override ``_EVIDENCE_TYPE_DELIVER_BY["coherence_collapse"]``).
_ACTUAL_EVENT = EVENT_EDIT_RESULT
# The minimum blind-overwrite count the V2 churn gate requires (``_ss_coherence_churn`` returns
# None for <=2 qualifying writes; the delivered claim only fires at >=3).
_MIN_CHURN = 3


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


def recompute_churn(
    write_steps: "Iterable[int]", pass_steps: "Iterable[int]"
) -> "int | None":
    """The PURE churn predicate — a faithful, self-contained replica of the seam's
    ``_ss_coherence_churn`` arithmetic (gt_mini_patch.py): the count of successful writes to
    the file SINCE the last passing test, or ``None`` when that count is <=2.

    ``write_steps`` is the (already path-filtered) list of action-count steps at which a
    byte-proven successful write to THIS file occurred; ``pass_steps`` the steps at which a
    test was observed PASSING. A passing test is a GREEN CHECKPOINT that resets the
    blind-overwrite baseline, so only writes STRICTLY AFTER the most recent green count. This
    is a total function of the recorded snapshot — no I/O, no globals, no time."""
    try:
        writes = sorted(int(w) for w in write_steps)
        passes = [int(s) for s in pass_steps]
    except (TypeError, ValueError):
        return None
    last_pass = max(passes) if passes else None
    if last_pass is not None:
        writes = [w for w in writes if w > last_pass]
    if len(writes) <= 2:
        return None
    return len(writes)


def _canonical_claim(rel: str, churn: int) -> str:
    """The exact FACTUAL-CLAIM substring the coherence producer builds into its steer body
    (``gt_mini_patch._coherence_collapse_candidate``): ``rewritten <basename> <N> times with
    no passing test between edits``. A pure function of ``(basename, churn)`` — the delivered
    steer must contain it, which binds the delivered N to the re-derived churn count."""
    return (
        f"rewritten {os.path.basename(rel)} {int(churn)} times "
        "with no passing test between edits"
    )


@dataclass(frozen=True)
class RecoveryDecisionInput:
    """An IMMUTABLE snapshot of the producer-owned counters the coherence-collapse decision
    consumed at production time. Audit-only; never model-facing."""

    rel: str
    churn: int
    write_steps: tuple[int, ...]
    pass_steps: tuple[int, ...]
    anchored: bool
    coherence_v2: bool
    actual_event: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel": self.rel,
            "churn": self.churn,
            "write_steps": list(self.write_steps),
            "pass_steps": list(self.pass_steps),
            "anchored": self.anchored,
            "coherence_v2": self.coherence_v2,
            "actual_event": self.actual_event,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_dict())


def build_recovery_candidate_input(
    *,
    rel: str,
    edit_events: "Iterable[tuple[str, int, bool]]",
    test_events: "Iterable[tuple[int, bool]]",
    anchored: bool,
    coherence_v2: bool,
    churn: int,
    actual_event: str = _ACTUAL_EVENT,
) -> RecoveryDecisionInput:
    """Bind the coherence producer's OWN edit/test event ledger to an immutable snapshot.

    ``edit_events`` is the seam's ``_ss_edit_events`` (``(rel, step, write_ok)`` rows);
    ``test_events`` the ``_ss_test_events`` (``(step, passed)`` rows). This filters them EXACTLY
    as ``_ss_coherence_churn`` does — successful writes to THIS EXACT path, and every passing
    test step — so the factory's :func:`recompute_churn` re-derives the same count the
    producer delivered. ``churn`` is the count the producer computed (recorded for the
    equality cross-check)."""
    write_steps = tuple(
        sorted(
            int(step)
            for (r, step, ok) in (edit_events or ())
            if r == rel and ok
        )
    )
    pass_steps = tuple(
        sorted(int(step) for (step, passed) in (test_events or ()) if passed)
    )
    return RecoveryDecisionInput(
        rel=str(rel or ""),
        churn=int(churn),
        write_steps=write_steps,
        pass_steps=pass_steps,
        anchored=bool(anchored),
        coherence_v2=bool(coherence_v2),
        actual_event=str(actual_event or ""),
    )


def finalize_recovery_attestation(
    candidate: RecoveryDecisionInput,
    *,
    producer_block: str,
    shipped_suffix: str,
    target: str,
    candidate_id: str,
    delivery_seal: str,
    actual_event: str = _ACTUAL_EVENT,
) -> FinalAttestationInputs:
    """Bind the exact delivered coherence steer to the producer's recorded churn decision.

    ``candidate``      the immutable :class:`RecoveryDecisionInput` snapshot.
    ``producer_block`` the EXACT rendered steer bytes (str) — the delivered candidate.
    ``shipped_suffix`` the boundary-joined bytes the lane actually appended.
    ``candidate_id``   the ledger row's ``candidate_id`` (envelope ``dedup_key``).
    ``delivery_seal``  the ledger row's ``content_sha256_16`` over the shipped bytes.

    Truth is PASS only when the recorded churn RE-DERIVES from the snapshot to the SAME
    count (>=3), that count is exactly the count the delivered steer CLAIMS, the fire was
    anchored, AND the delivered identity (candidate_id + seal) exactly matches the shipped
    bytes; otherwise UNMEASURED (correct-or-quiet). Raises only when the constructed
    attestation would be structurally invalid — the caller (audit persistence) swallows that
    and simply does not persist."""
    registration = registration_for(_EVIDENCE_TYPE)
    if registration is None:
        raise ValueError("coherence_collapse resolves to no registered fact class")

    input_bytes = candidate.canonical_bytes()
    rendered_bytes = (producer_block or "").encode("utf-8", "surrogatepass")
    shipped_bytes = (shipped_suffix or "").encode("utf-8", "surrogatepass")

    input_ref = ArtifactRef(
        kind="recovery_input",
        artifact_id="recovery-input.json",
        sha256=_sha(input_bytes),
        revision=f"candidate:{candidate_id}",
    )
    rendered_ref = ArtifactRef(
        kind="producer_candidate",
        artifact_id="producer-candidate.bin",
        sha256=_sha(rendered_bytes),
        revision=f"candidate:{candidate_id}",
    )
    shipped_ref = ArtifactRef(
        kind="shipped_suffix",
        artifact_id="shipped-suffix.bin",
        sha256=_sha(shipped_bytes),
        revision=f"seal:{delivery_seal}",
    )
    refs = tuple(sorted((input_ref, rendered_ref, shipped_ref)))
    by_id = {ref.artifact_id: ref for ref in refs}
    artifacts = (
        ("recovery-input.json", input_bytes),
        ("producer-candidate.bin", rendered_bytes),
        ("shipped-suffix.bin", shipped_bytes),
    )

    recomputed = recompute_churn(candidate.write_steps, candidate.pass_steps)
    claim = _canonical_claim(candidate.rel, candidate.churn)

    complete = bool(
        isinstance(candidate, RecoveryDecisionInput)
        and candidate.coherence_v2
        and isinstance(candidate.churn, int)
        and not isinstance(candidate.churn, bool)
        and candidate.churn >= _MIN_CHURN
        and recomputed == candidate.churn
        and candidate.anchored
        and bool(candidate.rel)
        and isinstance(producer_block, str)
        and claim in producer_block
        and isinstance(target, str)
        and target.replace("\\", "/") == candidate.rel.replace("\\", "/")
        and shipped_bytes in (rendered_bytes, b"\n" + rendered_bytes)
        and isinstance(delivery_seal, str)
        and delivery_seal == _sha(shipped_bytes)[:16]
        and isinstance(candidate_id, str)
        and candidate_id
        == lane_delivery_candidate_id(_LANE_KIND, target, shipped_suffix)
        and candidate.actual_event == _ACTUAL_EVENT
        and actual_event == _ACTUAL_EVENT
    )

    truth_proofs = (
        ProofRef("producer_churn", by_id["recovery-input.json"], "$.churn"),
        ProofRef("producer_render", by_id["producer-candidate.bin"], "$"),
        ProofRef("shipped_identity", by_id["shipped-suffix.bin"], "$"),
    )
    truth_predicate = PredicateAttestation(
        predicate_kind=TRUTH,
        predicate_id=f"{_EVIDENCE_TYPE}:truth",
        subject="exact final recovery (coherence_collapse) steer candidate",
        expectation=(
            "the delivered blind-overwrite count re-derives from the producer's own "
            "edit-event ledger and equals the count the steer claims"
        ),
        observation=(
            "churn re-derived from the recorded edit/test events reproduces the "
            "delivered claim"
            if complete
            else "steer not bound to a re-derived churn claim"
        ),
        verdict=PASS if complete else UNMEASURED,
        proof_refs=tuple(sorted(truth_proofs)) if complete else (),
    )
    # Freshness is honestly UNMEASURED: the snapshot carries action-count steps (episode
    # state), NOT a graph/content revision anchor, so no honest freshness proof exists.
    freshness_predicate = PredicateAttestation(
        predicate_kind=FRESHNESS,
        predicate_id=f"{_EVIDENCE_TYPE}:freshness",
        subject="exact final recovery (coherence_collapse) steer candidate",
        expectation="no graph/content revision anchor is recorded in the episode-state snapshot",
        observation="",
        verdict=UNMEASURED,
        proof_refs=(),
    )

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=_EVIDENCE_TYPE,
        runtime_producer_id=_PRODUCER_ID,
        registered_producer_id=registration.producer,
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        source_artifacts=refs,
        truth_predicates=(truth_predicate,),
        freshness_predicates=(freshness_predicate,),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            open_event=actual_event,
            required_event=required_event(_EVIDENCE_TYPE) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        raise ValueError("invalid recovery attestation: " + "|".join(errors))
    return FinalAttestationInputs(attestation, tuple(sorted(artifacts)))


__all__ = [
    "RecoveryDecisionInput",
    "build_recovery_candidate_input",
    "finalize_recovery_attestation",
    "recompute_churn",
]
