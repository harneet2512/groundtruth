"""RED-first unit tests for the submit-refusal producer-attestation factory.

The factory binds the exact delivered submit refusal to the PURE gate kernel's BLOCK
verdict and proves TRUTH by re-running the kernel on the verdict's own recorded inputs.
Truth is PASS only when the verdict is a real BLOCK that reproduces the same BLOCK AND
the delivered identity (candidate_id + seal) exactly matches the refusal bytes; UNMEASURED
otherwise. Freshness is honestly UNMEASURED (no patch/graph revision is recorded).

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION S2 — finalize_submit_refusal_attestation: drop the ``_reproduces_block(verdict)``
    leg of ``complete``. ``test_inconsistent_record_is_unmeasured`` then WRONGLY reports
    truth PASS for a BLOCK verdict whose own recorded inputs re-run to ALLOW (a
    non-reproducing verdict). Bite confirmed. (The outer ``verdict.allow is False`` leg is
    deliberate defense-in-depth and is redundant with ``_reproduces_block``'s own
    ``allow``-guard, so dropping IT alone does not bite — ``_reproduces_block`` is the load
    -bearing check.)

  * MUTATION S3 — drop the ``candidate_id == submit_refusal_candidate_id(refusal_text)``
    leg. ``test_candidate_mismatch_is_unmeasured`` then WRONGLY reports truth PASS for a
    forged candidate id. Bite confirmed.

  * MUTATION S-seal — drop the ``delivery_seal == _seal16(refusal_text)`` leg.
    ``test_seal_mismatch_is_unmeasured`` then WRONGLY reports truth PASS for a seal that
    does not cover the refusal bytes. Bite confirmed.
"""

from __future__ import annotations

import hashlib

from groundtruth.runtime.completion_control import submit_refusal_candidate_id
from groundtruth.runtime.producer_attestation import (
    FRESHNESS,
    PASS,
    TRUTH,
    UNMEASURED,
    validate,
)
from groundtruth.runtime.submit_attestation import finalize_submit_refusal_attestation
from groundtruth.runtime.submit_gate import GateVerdict, gate_verdict

_REFUSAL = "a covering test is failing — re-run the repo's own tests and fix before submitting"


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _identity(text: str = _REFUSAL) -> tuple[str, str]:
    return submit_refusal_candidate_id(text), _seal(text)


# --------------------------------------------------------------------------- #
# The complete, honest BLOCK -> truth PASS, freshness UNMEASURED.
# --------------------------------------------------------------------------- #
def test_covering_block_yields_truth_pass_freshness_unmeasured() -> None:
    verdict = gate_verdict(
        covering={"verdict": "fail", "reason": "red", "failing_test_names": ["t_x"]},
        hygiene=None,
        bounce_count=0,
        max_bounces=1,
    )
    assert verdict.allow is False and verdict.reason == "covering_test_failed"
    cid, seal = _identity()

    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal=seal
    )

    assert validate(final.attestation) == ()
    assert final.attestation.evidence_type == "submit_refusal"
    assert final.attestation.runtime_producer_id == "submit_gate"
    assert final.attestation.candidate_id == cid
    assert final.attestation.delivery_seal == seal
    assert final.attestation.truth_verdict == PASS
    # freshness is honestly UNMEASURED — a GateVerdict carries no patch/graph revision.
    assert final.attestation.freshness_verdict == UNMEASURED
    # the freshness predicate exists but is UNMEASURED with no proof refs.
    (fresh,) = final.attestation.freshness_predicates
    assert fresh.predicate_kind == FRESHNESS
    assert fresh.verdict == UNMEASURED and fresh.proof_refs == ()
    (truth,) = final.attestation.truth_predicates
    assert truth.predicate_kind == TRUTH and truth.proof_refs  # PASS carries proofs


def test_hygiene_block_yields_truth_pass() -> None:
    verdict = gate_verdict(
        covering=None,
        hygiene={"blocking": True, "reason": "too_broad", "detail": "12 files touched"},
        bounce_count=0,
        max_bounces=1,
    )
    assert verdict.allow is False and verdict.reason == "hygiene"
    cid, seal = _identity()

    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal=seal
    )
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS


# --------------------------------------------------------------------------- #
# Correct-or-quiet UNMEASURED cases (RED-first: none of these may claim PASS).
# --------------------------------------------------------------------------- #
def test_allow_verdict_is_unmeasured() -> None:
    # MUTATION S1 bites here.
    verdict = gate_verdict(covering={"verdict": "pass"}, hygiene=None)
    assert verdict.allow is True
    cid, seal = _identity()

    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal=seal
    )
    assert validate(final.attestation) == ()  # still a VALID (UNMEASURED) attestation
    assert final.attestation.truth_verdict == UNMEASURED


def test_inconsistent_record_is_unmeasured() -> None:
    # MUTATION S2 bites here: a BLOCK verdict whose OWN recorded inputs re-run to ALLOW
    # is not a reproduced block — truth must stay UNMEASURED.
    forged = GateVerdict(
        allow=False,
        reason="covering_test_failed",
        detail="forged",
        # the record says covering PASSED — re-running the kernel yields ALLOW, so this
        # verdict does not reproduce its own claimed BLOCK.
        record={
            "covering_verdict": "pass",
            "covering_reason": None,
            "hygiene_blocking": False,
            "hygiene_reason": None,
            "bounce_count": 0,
            "max_bounces": 1,
        },
    )
    cid, seal = _identity()

    final = finalize_submit_refusal_attestation(
        forged, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal=seal
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_candidate_mismatch_is_unmeasured() -> None:
    # MUTATION S3 bites here.
    verdict = gate_verdict(covering={"verdict": "fail"}, hygiene=None)
    _, seal = _identity()

    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id="forged:candidate", delivery_seal=seal
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_seal_mismatch_is_unmeasured() -> None:
    verdict = gate_verdict(covering={"verdict": "fail"}, hygiene=None)
    cid, _ = _identity()

    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal="0" * 16
    )
    # a well-formed but non-matching seal -> UNMEASURED (and the offline join would also
    # miss it, since the ledger row's content_sha256_16 is the real seal).
    assert final.attestation.truth_verdict == UNMEASURED


def test_wrong_event_is_unmeasured() -> None:
    verdict = gate_verdict(covering={"verdict": "fail"}, hygiene=None)
    cid, seal = _identity()

    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal=seal,
        actual_event="edit_result",  # not the submit boundary
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_malformed_seal_raises_and_is_never_persisted() -> None:
    # A structurally invalid seal (not 16 lower-hex) makes the attestation invalid; the
    # factory raises so the audit-persistence caller simply skips it (never a bad bundle).
    verdict = gate_verdict(covering={"verdict": "fail"}, hygiene=None)
    cid, _ = _identity()
    try:
        finalize_submit_refusal_attestation(
            verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal="NOT_HEX"
        )
    except ValueError as exc:
        assert "invalid submit refusal attestation" in str(exc)
    else:  # pragma: no cover - a malformed seal must raise
        raise AssertionError("expected a ValueError for a malformed delivery seal")
