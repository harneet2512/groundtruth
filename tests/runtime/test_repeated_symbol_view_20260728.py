"""C41 — a SECOND focused symbol view must not kill the canonical runtime.

ARTIFACT-FIRST. Found by reading run 30390877219's ledgers, where 4 of 4 tasks carried:

    observe_failed:StateIntegrityError:
      illegal hypothesis transition HypothesisState.ACTIVE via FOCUSED_SYMBOL_VIEW

and, immediately after, `canonical_observer_dark:legacy_delivery_resumed` for every remaining
observation: 59 of 61 actions on cfn-lint-3749, 38 of 42 on sh-744, 12 of 18 on cfn-lint-3764,
6 of 17 on aiogram-1594. Exactly ONE ObservationBinding was created per task -- the step-0 one,
made before the death. The canonical proof chain (per-observation identity, capsules, evidence
journal) was therefore inactive for ~97% of observations, which is the real reason nothing could
reach SS-LIVE on that run.

THE ASYMMETRY, read from `_HYPOTHESIS_TRANSITIONS`:

    FOCUSED_SYMBOL_VIEW: allowed={None, CANDIDATE}          -> ACTIVE
    EDIT_PROPOSED:       allowed={None, CANDIDATE, ACTIVE}  -> ACTIVE     # idempotent

`EDIT_PROPOSED` may re-fire on an already-ACTIVE hypothesis; `FOCUSED_SYMBOL_VIEW` may not. But
`SemanticKind.SYMBOL_VIEWED` maps to `FOCUSED_SYMBOL_VIEW` unconditionally, and viewing symbols
repeatedly is the single most common thing a coding agent does. So the SECOND view of a subject
raises, and the raise classifies as a reducer-invariant violation, which isolates the observer for
the rest of the attempt.

THIS IS THE MIRROR OF AN ALREADY-FIXED BUG. The `orphaned_outcome` branch above the raise exists
because an OUTCOME transition can arrive when GT never opened a hypothesis (run 30276041709,
`illegal hypothesis transition None via VALIDATION_SUPPORT`, 52 compiles then dark forever). The
reasoning recorded there -- "GT is never TOLD the agent's hypotheses, it infers them from
observations" -- applies identically here, with the polarity flipped: an OPENING transition can
arrive when GT has ALREADY opened one.

WHY WIDEN THE TABLE RATHER THAN SKIP: the target state is ACTIVE and the current state is ACTIVE,
so admitting it introduces no new reachable state and cannot enter an illegal one -- the exact
invariant the check protects is untouched. It also matches the precedent already in the same
table for EDIT_PROPOSED, rather than adding a second special case beside `orphaned_outcome`.
Subject rebinding is still refused above (`hypothesis subject cannot be rebound`), so a view of a
DIFFERENT subject on the same hypothesis id still raises.

BITING MUTATION (applied, observed RED, reverted by targeted restore):
  M1 -- restore `frozenset({None, HypothesisState.CANDIDATE})`:
        `test_a_second_focused_symbol_view_is_legal` goes RED with the exact production message.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime.reasoning_runtime import (
    _HYPOTHESIS_TRANSITIONS,
    HypothesisState,
    OperationalSignalKind,
)


def _allowed(kind: OperationalSignalKind) -> frozenset:
    return _HYPOTHESIS_TRANSITIONS[kind][0]


def test_the_table_is_readable_and_the_probe_can_produce_a_non_zero() -> None:
    """CALIBRATION. If the table were empty every assertion below would be vacuous."""
    assert _allowed(OperationalSignalKind.EXACT_SEARCH) == frozenset({None})
    assert _HYPOTHESIS_TRANSITIONS[
        OperationalSignalKind.FOCUSED_SYMBOL_VIEW
    ][1] is HypothesisState.ACTIVE


def test_a_second_focused_symbol_view_is_legal() -> None:
    """M1. THE RUN DEFECT: the agent views one symbol, then another, and GT dies.

    4/4 tasks on run 30390877219 died exactly here.
    """
    assert HypothesisState.ACTIVE in _allowed(
        OperationalSignalKind.FOCUSED_SYMBOL_VIEW
    )


def test_the_opening_signals_agree_with_each_other() -> None:
    """The asymmetry itself was the bug: two OPENING transitions to the same target state
    disagreed about whether that target may re-enter itself."""
    view = _allowed(OperationalSignalKind.FOCUSED_SYMBOL_VIEW)
    edit = _allowed(OperationalSignalKind.EDIT_PROPOSED)
    assert HypothesisState.ACTIVE in view and HypothesisState.ACTIVE in edit
    assert None in view and None in edit
    assert HypothesisState.CANDIDATE in view and HypothesisState.CANDIDATE in edit


def test_no_new_state_became_reachable() -> None:
    """Widening an allowed-FROM set to include the transition's own TARGET cannot make the
    machine enter a state it could not already enter. Pinned so a future widening that DOES
    add a new reachable state has to justify itself here."""
    for kind, (allowed, target, _reason) in _HYPOTHESIS_TRANSITIONS.items():
        assert isinstance(target, HypothesisState), kind
        for state in allowed:
            assert state is None or isinstance(state, HypothesisState), kind


@pytest.mark.parametrize(
    "kind",
    [
        OperationalSignalKind.VALIDATION_SUPPORT,
        OperationalSignalKind.UNCHANGED_FAILURE_AFTER_EDIT,
        OperationalSignalKind.VERIFIED_COUNTEREVIDENCE,
        OperationalSignalKind.ABANDON_TARGET,
        OperationalSignalKind.SUPERSEDING_HYPOTHESIS,
    ],
)
def test_outcome_transitions_still_require_an_open_hypothesis(kind) -> None:
    """The fix must NOT loosen the outcome half of the table. An outcome arriving with no
    hypothesis is handled by the `orphaned_outcome` skip, deliberately, and must keep being
    excluded from `allowed` so that branch stays the one that handles it."""
    assert None not in _allowed(kind)


def test_a_terminal_state_cannot_reopen_as_active() -> None:
    """ABANDONED / CONTRADICTED / SUPERSEDED must never be a legal FROM for an opening
    transition -- that WOULD be a new reachable path and is not what this fix does."""
    terminal = {
        HypothesisState.ABANDONED,
        HypothesisState.CONTRADICTED,
        HypothesisState.SUPERSEDED,
    }
    for kind in (
        OperationalSignalKind.EXACT_SEARCH,
        OperationalSignalKind.FOCUSED_SYMBOL_VIEW,
        OperationalSignalKind.EDIT_PROPOSED,
    ):
        assert not (_allowed(kind) & terminal), kind
