"""D-J (run6 batch6, haystack-8997): the ledger logged '[not exercised] fired
for a scenario exercised earlier in the same run; root cause: exercise-state
update lags delivery.'

Reading the LIVE path falsified that root cause:
  - gt_mini_patch._ss_record_behavioral_proof (line ~18740) appends the current
    turn's probe event BEFORE _v2_obligation_result_ready()/_v2_obligation_truth()
    (line ~18848) reads it. Record precedes compute -> there is NO same-turn lag.
  - The only mechanism that yields UNEXERCISED after an earlier exercise is the
    FRESHNESS gate: obligation_truth_statuses credits an exercise only when
    exercise.turn > after_turn, where after_turn = _ss_last_relevant_edit_turn.
    An edit AFTER the test invalidates that test (docstring: "Opaque writes
    invalidate conservatively").

This is correct-or-quiet TOWARD SAFETY: a false "[not exercised]" costs a
re-test (safe); the opposite error (false "[exercised]") would let the agent
finalize unverified work — exactly what this verification feature prevents.
This test pins that intended behavior so it cannot silently regress.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from groundtruth.runtime.obligations import (  # noqa: E402
    ExerciseEvidence,
    ObligationTruthState,
    _credit_eligible_symbols,
    _normalize_subjects,
    obligation_truth_statuses,
)


def _view(idx: int, sym: str):
    return SimpleNamespace(
        idx=idx,
        region="normative",
        subject_symbols={sym},
        verbatim=f"the {sym} path must be preserved",
        sym_parts=frozenset({sym}),
    )


def _exercise_for(view, turn: int) -> ExerciseEvidence:
    key = _normalize_subjects(_credit_eligible_symbols(view))
    return ExerciseEvidence(key, turn, "related_execution", 0)


def test_exercise_counts_when_no_later_edit():
    # exercise at turn 5, last relevant edit at turn 3 (before the test) -> EXERCISED
    view = _view(0, "process_config")
    rows = obligation_truth_statuses(
        [view], edited_tokens={"process_config"},
        exercise_evidence=[_exercise_for(view, turn=5)],
        after_turn_by_id={0: 3},
    )
    assert rows[0].state is ObligationTruthState.EXERCISED_UNPROVEN


def test_exercise_invalidated_by_a_later_edit_is_by_design():
    # SAME exercise at turn 5, but a relevant edit at turn 6 (AFTER the test) ->
    # freshness correctly demotes to UNEXERCISED (must re-test after editing)
    view = _view(0, "process_config")
    rows = obligation_truth_statuses(
        [view], edited_tokens={"process_config"},
        exercise_evidence=[_exercise_for(view, turn=5)],
        after_turn_by_id={0: 6},
    )
    assert rows[0].state is ObligationTruthState.UNEXERCISED, (
        "an edit after the exercise must invalidate it — failing toward re-test "
        "is the safe side for a verification feature")


def test_record_precedes_compute_invariant_documented():
    # Not a runtime call (the ordering lives in the live hook), but pin the fact
    # that the truth projection reads whatever evidence list it is given — so if
    # the caller records the current turn's event first (it does, gt_mini_patch
    # ~18740 before ~18848), the current exercise is visible at compute time.
    view = _view(0, "resolve_value")
    rows = obligation_truth_statuses(
        [view], edited_tokens=set(),
        exercise_evidence=[_exercise_for(view, turn=9)],
        after_turn_by_id={0: -1},
    )
    assert rows[0].state is ObligationTruthState.EXERCISED_UNPROVEN
