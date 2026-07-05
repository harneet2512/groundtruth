"""F1 (Fable 2026-07-05): the post-edit `semantic_drift` steer must be a real PayloadKind.

The producer appends candidates with the literal kind "semantic_drift"
(gt_mini_patch `cands.append((..., "semantic_drift", ...))`). Before the fix that string
was absent from PayloadKind and from every PHASE_POLICY set, so `phase_allows` returned
False on EVERY phase -> the steer was dead-on-arrival (wrong_phase forever). This pins that
it is now allowed on the phases where a guard/return-deletion is observed (EDIT + VERIFY)
and bound to the POST_EDIT event.

RED on the pre-fix code: `phase_allows("semantic_drift", Phase.EDIT)` is False.
"""
from __future__ import annotations

from groundtruth.runtime.context_policy import (
    EVENT_BOUND_PAYLOADS,
    PHASE_POLICY,
    Event,
    PayloadKind,
    Phase,
    phase_allows,
)

_SD = "semantic_drift"


def test_semantic_drift_is_a_payloadkind():
    assert PayloadKind.SEMANTIC_DRIFT.value == _SD


def test_semantic_drift_allowed_on_edit_and_verify():
    assert phase_allows(_SD, Phase.EDIT) is True   # RED pre-fix: wrong_phase (False)
    assert phase_allows(_SD, Phase.VERIFY) is True
    assert _SD in PHASE_POLICY[Phase.EDIT]
    assert _SD in PHASE_POLICY[Phase.VERIFY]


def test_semantic_drift_bound_to_post_edit_event():
    assert _SD in EVENT_BOUND_PAYLOADS[Event.POST_EDIT]


def test_semantic_drift_not_leaked_into_unrelated_phases():
    # correct-or-quiet: a guard/return-deletion is not an ORIENT/VIEW/SUBMIT concern
    assert _SD not in PHASE_POLICY[Phase.ORIENT]
    assert _SD not in PHASE_POLICY[Phase.VIEW]
