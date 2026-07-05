"""F4 (Fable 2026-07-05): the consensus.scope completeness steer must be phase-allowed at
VERIFY.

The producer fires on the review predicate (edited files + non-edit streak >= 3), which is
exactly when derive_phase reaches VERIFY. But the payload was event-bound ONLY to
REVIEW_TRANSITION, and _current_event returns POST_VIEW/POST_EDIT on those turns (they outrank
the review event in the elif chain), so the produced candidate hit wrong_phase and was starved.
Allowing it at VERIFY delivers it via the phase gate regardless of the event label.

RED on the pre-fix policy: phase_allows("consensus.scope", Phase.VERIFY) is False.
"""
from __future__ import annotations

from groundtruth.runtime.context_policy import (
    PHASE_POLICY,
    PayloadKind,
    Phase,
    phase_allows,
)

_SCOPE = "consensus.scope"


def test_scope_completeness_value():
    assert PayloadKind.SCOPE_COMPLETENESS.value == _SCOPE


def test_scope_completeness_allowed_at_verify():
    assert phase_allows(_SCOPE, Phase.VERIFY) is True   # RED pre-fix: wrong_phase (False)
    assert _SCOPE in PHASE_POLICY[Phase.VERIFY]


def test_scope_completeness_not_leaked_into_edit_orient_view():
    # correct-or-quiet: completeness is a verify/review concern, not edit/orient/view.
    assert _SCOPE not in PHASE_POLICY[Phase.EDIT]
    assert _SCOPE not in PHASE_POLICY[Phase.ORIENT]
    assert _SCOPE not in PHASE_POLICY[Phase.VIEW]
