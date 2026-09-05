"""Producer-owned truth attestation for the submit-gate refusal (``submit_refusal``).

The submit gate (:func:`groundtruth.runtime.submit_gate.gate_verdict`) is a PURE,
deterministic decision head: ALLOW | BLOCK from recorded facts only (no LLM, no network,
no task IDs, no gold labels). When it BLOCKs and the seam renders + delivers the native
pre-commit refusal, this factory binds that exact delivered candidate to the gate's OWN
:class:`~groundtruth.runtime.submit_gate.GateVerdict`, and proves TRUTH by RE-RUNNING the
kernel on the verdict's own recorded inputs and confirming it reproduces the SAME BLOCK
(``allow is False`` and the same machine reason). This is the covering_red epistemic
standard applied to the gate: the attestation binds the producer's recorded decision +
its inputs, and re-derives them deterministically — it does not re-run the underlying
tests.

Producer-owned + deterministically re-verifiable at production time; correct-or-quiet
(UNMEASURED) on any incomplete or mismatched join. It performs no I/O, reads no flags,
and changes no delivered bytes.

FRESHNESS is honestly UNMEASURED. The registry names ``patch_rev``+``graph_rev`` as this
fact's freshness dependencies, but a ``GateVerdict`` records the covering/hygiene verdicts
WITHOUT the patch/graph revision, so no honest freshness proof exists here. Rather than
fabricate one (the ea0eb16c0 lesson), the freshness predicate is UNMEASURED — truth and
authority (the two ``correct_info`` legs) are measured; freshness stays honestly dark.
"""

from __future__ import annotations

import hashlib
import json

from .completion_control import submit_refusal_candidate_id
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
from .submit_gate import GateVerdict, gate_verdict

_EVIDENCE_TYPE = "submit_refusal"
_PRODUCER_ID = "submit_gate"
_ACTUAL_EVENT = "submit"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal16(text: str) -> str:
    """The delivery seal the runtime ledger stamps for a refusal block: the truncated
    sha256 over the exact shipped bytes, with the SAME ``surrogatepass`` encoding the
    ledger writer uses (gt_mini_patch ``_runtime_ledger_record`` content seal)."""
    return _sha(text.encode("utf-8", "surrogatepass"))[:16]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reproduces_block(verdict: GateVerdict) -> bool:
    """Re-run the PURE gate kernel on the verdict's OWN recorded inputs and confirm it
    reproduces the same BLOCK (``allow is False`` and the identical machine reason).

    ``GateVerdict.record`` carries every kernel input verbatim (covering verdict/reason,
    hygiene blocking/reason, bounce_count, max_bounces), so a replay is a total,
    deterministic function of the producer's own recorded evidence. A missing/poisoned
    record, a raise, or any divergence returns ``False`` (correct-or-quiet)."""
    if not isinstance(verdict, GateVerdict) or verdict.allow is not False:
        return False
    record = verdict.record if isinstance(verdict.record, dict) else {}
    try:
        bounce = record.get("bounce_count", 0)
        cap = record.get("max_bounces", 1)
        if not isinstance(bounce, int) or isinstance(bounce, bool):
            return False
        if not isinstance(cap, int) or isinstance(cap, bool):
            return False
        replay = gate_verdict(
            covering={
                "verdict": record.get("covering_verdict"),
                "reason": record.get("covering_reason"),
                "failing_test_names": record.get("covering_failing_names") or [],
            },
            hygiene={
                "blocking": bool(record.get("hygiene_blocking")),
                "reason": record.get("hygiene_reason"),
            },
            bounce_count=bounce,
            max_bounces=cap,
        )
    except Exception:  # noqa: BLE001 -- a replay fault is correct-or-quiet UNMEASURED
        return False
    return replay.allow is False and replay.reason == verdict.reason


def finalize_submit_refusal_attestation(
    verdict: GateVerdict,
    *,
    refusal_text: str,
    candidate_id: str,
    delivery_seal: str,
    actual_event: str = _ACTUAL_EVENT,
) -> FinalAttestationInputs:
    """Bind the exact delivered submit refusal to the pure gate's BLOCK verdict.

    ``verdict``       the ``GateVerdict`` the seam produced (``safe_gate_verdict``).
    ``refusal_text``  the EXACT shipped refusal bytes (str) — the delivered candidate.
    ``candidate_id``  the ledger row's ``candidate_id`` (``submit_refusal_candidate_id``).
    ``delivery_seal`` the ledger row's ``content_sha256_16`` over the shipped bytes.

    Returns a validated :class:`FinalAttestationInputs`. Truth is PASS only when the
    verdict is a real BLOCK that re-runs to the same BLOCK AND the delivered identity
    (candidate_id + seal) exactly matches ``refusal_text``; otherwise UNMEASURED. Raises
    only when the constructed attestation would be structurally invalid (a malformed
    seal) — the caller (audit persistence) swallows that and simply does not persist."""
    registration = registration_for(_EVIDENCE_TYPE)
    if registration is None:
        raise ValueError("submit_refusal is not a registered fact class")

    refusal_bytes = (refusal_text or "").encode("utf-8", "surrogatepass")
    verdict_payload = {
        "allow": bool(getattr(verdict, "allow", True)),
        "reason": str(getattr(verdict, "reason", "")),
        "record": dict(verdict.record)
        if isinstance(getattr(verdict, "record", None), dict)
        else {},
    }
    verdict_bytes = _canonical(verdict_payload)

    verdict_ref = ArtifactRef(
        kind="gate_verdict",
        artifact_id="gate-verdict.json",
        sha256=_sha(verdict_bytes),
        revision=f"candidate:{candidate_id}",
    )
    rendered_ref = ArtifactRef(
        kind="rendered_candidate",
        artifact_id="rendered-refusal.bin",
        sha256=_sha(refusal_bytes),
        revision=f"seal:{delivery_seal}",
    )
    refs = tuple(sorted((verdict_ref, rendered_ref)))
    artifacts = (
        ("gate-verdict.json", verdict_bytes),
        ("rendered-refusal.bin", refusal_bytes),
    )

    complete = bool(
        isinstance(verdict, GateVerdict)
        and verdict.allow is False
        and _reproduces_block(verdict)
        and isinstance(refusal_text, str)
        and refusal_text != ""
        and isinstance(candidate_id, str)
        and candidate_id == submit_refusal_candidate_id(refusal_text)
        and isinstance(delivery_seal, str)
        and delivery_seal == _seal16(refusal_text)
        and actual_event == _ACTUAL_EVENT
    )

    truth_proofs = (
        ProofRef("gate_output", verdict_ref, "$.allow"),
        ProofRef("gate_output", verdict_ref, "$.reason"),
        ProofRef("shipped_identity", rendered_ref, "$"),
    )
    truth_predicate = PredicateAttestation(
        predicate_kind=TRUTH,
        predicate_id=f"{_EVIDENCE_TYPE}:truth",
        subject="exact final submit refusal candidate",
        expectation="a pure submit-gate BLOCK re-derived from the recorded submit facts",
        observation=(
            "gate BLOCK reproduced from the verdict's own recorded inputs"
            if complete
            else "refusal not bound to a reproduced gate BLOCK"
        ),
        verdict=PASS if complete else UNMEASURED,
        proof_refs=tuple(sorted(truth_proofs)) if complete else (),
    )
    # Freshness is honestly UNMEASURED: a GateVerdict does not carry the patch/graph
    # revision the registry names as this fact's freshness deps (see module docstring).
    freshness_predicate = PredicateAttestation(
        predicate_kind=FRESHNESS,
        predicate_id=f"{_EVIDENCE_TYPE}:freshness",
        subject="exact final submit refusal candidate",
        expectation="no patch/graph revision is recorded in the gate verdict",
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
        raise ValueError("invalid submit refusal attestation: " + "|".join(errors))
    return FinalAttestationInputs(attestation, tuple(sorted(artifacts)))


__all__ = ["finalize_submit_refusal_attestation"]
