"""B-REC — producer-owned truth attestation for the recovery FACT class (coherence_collapse).

TTD (red-first): the truth is UNMEASURED until a validated producer attestation joins a
DELIVERED ledger row on the exact (candidate_id, delivery_seal) identity. These tests pin:
  * ABSENT   -> join yields no "recovery" truth (UNMEASURED)                       [red]
  * PRESENT  -> validated attestation + delivered row -> truth PASS + authority     [green]
  * FORGED   -> a tampered delivery_seal never joins                                [mutation]
  * TAMPERED -> a churn snapshot that does not re-derive the delivered N -> UNMEASURED[mutation]
  * PREDICATE-> recompute_churn mutation breaks reproduction (the pure predicate bites)[mutation]

The fixtures reproduce the EXACT live delivery of amoffat__sh-744 (churn=3, sh.py, the sealed
candidate_id 9df0b6d122f19790 / seal 2874a992fe6fede4) so the join identity is real, not toy.
"""

from __future__ import annotations

import hashlib

import pytest

from groundtruth.runtime.lane_attestation import lane_delivery_candidate_id
from groundtruth.runtime.producer_attestation import PASS, UNMEASURED, validate
from groundtruth.runtime.recovery_attestation import (
    RecoveryDecisionInput,
    build_recovery_candidate_input,
    finalize_recovery_attestation,
    recompute_churn,
)
from groundtruth.runtime.fact_registry import registration_for
from scripts.swebench.attestation_join import ATTESTED_FACT_CLASSES, join_truth

# --- the live amoffat__sh-744 coherence delivery (byte-exact) --------------------------
_BODY = (
    "you have rewritten sh.py 3 times with no passing test between edits - you are "
    "overwriting your own work blind. Run targeted verification FIRST to see what is "
    "actually failing, then make one targeted edit."
)
_SHIPPED = "\n" + _BODY + "\n"          # native block; starts with \n -> no extra boundary
_TARGET = "sh.py"
_LIVE_CANDIDATE_ID = "9df0b6d122f19790"
_LIVE_SEAL = "2874a992fe6fede4"


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _live_input() -> RecoveryDecisionInput:
    # 3 successful writes to sh.py at steps 35/36/37, no passing test between -> churn 3.
    return build_recovery_candidate_input(
        rel=_TARGET,
        edit_events=[("sh.py", 35, True), ("sh.py", 36, True), ("sh.py", 37, True),
                     ("other.py", 20, True)],
        test_events=[(30, False), (33, False)],
        anchored=True,
        coherence_v2=True,
        churn=3,
    )


def _delivered_row(candidate_id: str, seal: str) -> dict:
    # J6: a detect.coherence row carries a registered ``profile_member`` owner
    # (GT_SS_COHERENCE_V2) and NO typed FACT lineage by product decision (P4) — exactly what
    # the live seam stamps (gt_mini_patch._lane_delivery_extra -> _exact_profile_delivery_extra).
    # The truth join accepts a registered profile_member owner as a valid attribution witness.
    return {
        "layer": "detect.coherence",
        "outcome": "delivered",
        "candidate_id": candidate_id,
        "content_sha256_16": seal,
        "profile_member": "GT_SS_COHERENCE_V2",
    }


# --- fixture sanity: the reconstructed identity IS the live one ------------------------
def test_fixture_matches_live_identity() -> None:
    assert _seal(_SHIPPED) == _LIVE_SEAL
    assert lane_delivery_candidate_id("detect.coherence", _TARGET, _SHIPPED) == _LIVE_CANDIDATE_ID


# --- the pure predicate ----------------------------------------------------------------
def test_recompute_churn_since_last_pass() -> None:
    # writes 35/36/37, last pass at 33 -> all three count -> 3
    assert recompute_churn((35, 36, 37), (30, 33)) == 3
    # a green at 36 resets: only 37 remains -> <=2 -> None
    assert recompute_churn((35, 36, 37), (36,)) is None
    # <=2 writes -> None
    assert recompute_churn((35, 36), ()) is None


# --- RED: absent attestation -> UNMEASURED --------------------------------------------
def test_absent_attestation_is_unmeasured() -> None:
    joins = join_truth([], [_delivered_row(_LIVE_CANDIDATE_ID, _LIVE_SEAL)])
    assert "recovery" not in joins  # no attestation -> class absent -> UNMEASURED


# --- GREEN: present + joined -> truth PASS + authority --------------------------------
def test_present_and_joined_is_measured_pass() -> None:
    final = finalize_recovery_attestation(
        _live_input(),
        producer_block=_SHIPPED,
        shipped_suffix=_SHIPPED,
        target=_TARGET,
        candidate_id=_LIVE_CANDIDATE_ID,
        delivery_seal=_LIVE_SEAL,
    )
    att = final.attestation
    assert validate(att) == ()
    assert att.evidence_type == "coherence_collapse"
    assert att.runtime_producer_id == "ss_coherence_v2"
    assert att.registered_producer_id == registration_for("coherence_collapse").producer  # governor
    assert att.truth_verdict == PASS
    assert att.freshness_verdict == UNMEASURED   # honest-dark

    joins = join_truth([att], [_delivered_row(_LIVE_CANDIDATE_ID, _LIVE_SEAL)])
    assert "recovery" in joins                    # coherence_collapse -> recovery class
    tj = joins["recovery"]
    assert tj.truth is True
    assert tj.authority is True
    assert tj.freshness is None                   # UNMEASURED
    assert tj.attestation_count == 1
    assert "recovery" in ATTESTED_FACT_CLASSES


# --- MUTATION 1: forged/tampered delivery seal never joins ----------------------------
def test_forged_seal_does_not_join() -> None:
    att = finalize_recovery_attestation(
        _live_input(), producer_block=_SHIPPED, shipped_suffix=_SHIPPED,
        target=_TARGET, candidate_id=_LIVE_CANDIDATE_ID, delivery_seal=_LIVE_SEAL,
    ).attestation
    forged = "0" * 16
    joins = join_truth([att], [_delivered_row(_LIVE_CANDIDATE_ID, forged)])
    assert "recovery" not in joins   # seal mismatch -> no exact join -> UNMEASURED


# --- MUTATION 2: a tampered churn snapshot cannot claim truth --------------------------
def test_tampered_churn_snapshot_is_unmeasured() -> None:
    # The snapshot claims churn=4 but its own edit events re-derive to 3 (and the body says 3):
    # recompute != claimed churn AND the "rewritten sh.py 4 times" claim is absent -> UNMEASURED.
    tampered = RecoveryDecisionInput(
        rel=_TARGET, churn=4,
        write_steps=(35, 36, 37), pass_steps=(),
        anchored=True, coherence_v2=True, actual_event="edit_result",
    )
    final = finalize_recovery_attestation(
        tampered, producer_block=_SHIPPED, shipped_suffix=_SHIPPED,
        target=_TARGET, candidate_id=_LIVE_CANDIDATE_ID, delivery_seal=_LIVE_SEAL,
    )
    assert final.attestation.truth_verdict == UNMEASURED
    joins = join_truth([final.attestation],
                       [_delivered_row(_LIVE_CANDIDATE_ID, _LIVE_SEAL)])
    # It still joins (identity matches) but contributes an UNMEASURED truth, not True.
    assert joins["recovery"].truth is None


# --- MUTATION 3: the pure predicate is load-bearing (drop the pass-reset semantics) ---
def test_predicate_pass_reset_is_load_bearing() -> None:
    # If recompute_churn ignored the passing-test reset, a green at 36 would still count 3
    # writes; the correct predicate returns None (only step 37 survives the reset) -> the
    # snapshot's claimed churn=3 would NOT re-derive -> UNMEASURED. This proves the reset
    # clause bites (mutating it to "count all writes" would wrongly PASS).
    reset_input = RecoveryDecisionInput(
        rel=_TARGET, churn=3,
        write_steps=(35, 36, 37), pass_steps=(36,),  # green checkpoint at 36
        anchored=True, coherence_v2=True, actual_event="edit_result",
    )
    assert recompute_churn(reset_input.write_steps, reset_input.pass_steps) is None
    final = finalize_recovery_attestation(
        reset_input, producer_block=_SHIPPED, shipped_suffix=_SHIPPED,
        target=_TARGET, candidate_id=_LIVE_CANDIDATE_ID, delivery_seal=_LIVE_SEAL,
    )
    assert final.attestation.truth_verdict == UNMEASURED


# --- MUTATION 4: churn below the >=3 blind-overwrite floor stays quiet -----------------
def test_churn_below_floor_is_unmeasured() -> None:
    low = RecoveryDecisionInput(
        rel=_TARGET, churn=2, write_steps=(36, 37), pass_steps=(),
        anchored=True, coherence_v2=True, actual_event="edit_result",
    )
    body2 = _BODY.replace("3 times", "2 times")
    shipped2 = "\n" + body2 + "\n"
    final = finalize_recovery_attestation(
        low, producer_block=shipped2, shipped_suffix=shipped2, target=_TARGET,
        candidate_id=lane_delivery_candidate_id("detect.coherence", _TARGET, shipped2),
        delivery_seal=_seal(shipped2),
    )
    assert final.attestation.truth_verdict == UNMEASURED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
