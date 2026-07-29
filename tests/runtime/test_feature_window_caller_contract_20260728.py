"""C11 follow-on: the commitment window is FALSIFIABLE for `caller_contract`.

The scheduler's release test was the literal constant ``CommitmentWindowState.OPEN``, so
``release_allowed == relevant`` and timing was algebraically eliminated. ``FeatureWindow``
restores a real three-point window. ss_gate drives the real seam but cannot reach this
decision, so the window's own behaviour is asserted here and only here.

Two things must hold and are easy to get wrong:
  1. A passed window routes to HELD (recoverable), NEVER to the terminal EXPIRED.
  2. The other sixteen DIRECT contracts declare no window and therefore keep the exact
     former constant.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime import fact_registry
from groundtruth.runtime import reasoning_runtime as rr


ALL_17 = sorted(rr._FACT_DECISION_CONTRACTS) + sorted(rr._CAP_FACT_BINDING)


def test_only_caller_contract_declares_a_window() -> None:
    """The None branch is the byte-identity guarantee for the other sixteen."""
    windowed = {
        feature_id
        for feature_id in ALL_17
        if rr.feature_contract_for(feature_id).window is not None
    }
    assert windowed == {"caller_contract"}


def test_the_window_is_the_registrys_own_three_points() -> None:
    """Every boundary is READ from the live registry, never written down in the contract."""
    window = rr.feature_contract_for("caller_contract").window
    assert window.earliest_event == fact_registry.earliest_event_for(
        "caller_contract_search"
    )
    assert window.deliver_by == fact_registry.required_event("caller_contract")
    assert window.corrective_boundary == fact_registry.required_event("caller_break")
    assert (window.earliest_event, window.deliver_by, window.corrective_boundary) == (
        "search_result",
        "file_view",
        "edit_result",
    )


def test_boundary_decision_table_matches_the_live_reducer_chain() -> None:
    """The projection must be a CHECKED rule, not a convention.

    Drives boundary -> reduce_event -> phase -> _active_decision for every boundary in
    the table and asserts the module's table agrees with what the live runtime opens.
    """
    from tests.runtime.test_all17_oracle_eligibility_20260726 import (
        BOUNDARY_OUTCOME,
        _open_decision_at,
    )

    assert set(rr._BOUNDARY_DECISION) == set(BOUNDARY_OUTCOME), (
        "the window boundary table and the live boundary inventory disagree"
    )
    for boundary in sorted(BOUNDARY_OUTCOME):
        assert rr._BOUNDARY_DECISION[boundary] is _open_decision_at(boundary).context, (
            f"table says {boundary} opens {rr._BOUNDARY_DECISION[boundary]}, "
            f"live chain opens {_open_decision_at(boundary).context}"
        )


@pytest.mark.parametrize(
    ("observed", "expected"),
    (
        # search_result..edit_result is the window; before it the fact is premature,
        # after it the decision it was meant to shape is committed.
        (rr.DecisionContext.SOURCE_TARGET_SELECTION, rr.CommitmentWindowState.OPEN),
        (rr.DecisionContext.SOURCE_UNDERSTANDING, rr.CommitmentWindowState.OPEN),
        (rr.DecisionContext.PATCH_CONSTRUCTION, rr.CommitmentWindowState.OPEN),
        (rr.DecisionContext.PATCH_PROPAGATION, rr.CommitmentWindowState.COMMITTED),
        (rr.DecisionContext.COMPLETION, rr.CommitmentWindowState.COMMITTED),
        # Off-spine and absent both fail OPEN: an unrecognised position may never withhold.
        (rr.DecisionContext.FAILURE_RECOVERY, rr.CommitmentWindowState.OPEN),
        (None, rr.CommitmentWindowState.OPEN),
    ),
)
def test_resolve_is_falsifiable_across_the_spine(observed, expected) -> None:
    window = rr.feature_contract_for("caller_contract").window
    assert window.resolve(observed) is expected


def test_resolve_can_report_not_open() -> None:
    """A window that opens later must be able to say NOT_OPEN, or it is not a window."""
    late = rr.FeatureWindow(
        earliest_event="edit_result",
        deliver_by="edit_result",
        corrective_boundary="test_result",
    )
    assert (
        late.resolve(rr.DecisionContext.SOURCE_TARGET_SELECTION)
        is rr.CommitmentWindowState.NOT_OPEN
    )
    assert (
        late.resolve(rr.DecisionContext.PATCH_CONSTRUCTION)
        is rr.CommitmentWindowState.OPEN
    )


def test_resolve_never_emits_closed() -> None:
    """CLOSED is the state that routes to the unrecoverable EXPIRED. Never emit it."""
    window = rr.feature_contract_for("caller_contract").window
    observed = list(rr.DecisionContext) + [None]
    assert all(
        window.resolve(value) is not rr.CommitmentWindowState.CLOSED
        for value in observed
    )


def _scheduler_eval(decision_context, *, role_driven):
    from tests.runtime.test_temporal_feature_contract_runtime import (
        _context as _temporal_context,
        _decision as _temporal_decision,
        _evidence as _temporal_evidence,
    )

    contract = rr.feature_contract_for("caller_contract")
    return rr._evaluate_current_decision_contract(
        contract,
        _temporal_evidence(lifecycle=rr.EvidenceLifecycle.READY),
        _temporal_context(
            contract,
            active_decision=_temporal_decision(context=decision_context),
        ),
        role_driven=role_driven,
    )


def test_a_passed_window_holds_the_record_it_never_expires_it() -> None:
    """THE NON-NEGOTIABLE, at the scheduler policy that actually decides release.

    Asserted under ``role_driven=True`` deliberately. That is the configuration in which
    the window is REACHABLE (see the dominance test below), so this is the non-vacuous
    form: relevance passes, the window is consulted, and it withholds.
    """
    inside = _scheduler_eval(
        rr.DecisionContext.PATCH_CONSTRUCTION, role_driven=True
    )
    assert inside.relevant is True
    assert inside.release_allowed is True
    assert inside.next_lifecycle is rr.EvidenceLifecycle.RELEASED

    for past in (
        rr.DecisionContext.PATCH_PROPAGATION,
        rr.DecisionContext.COMPLETION,
    ):
        evaluation = _scheduler_eval(past, role_driven=True)
        # Relevance PASSED, so the withholding is attributable to the window alone.
        assert evaluation.relevant is True, f"{past} short-circuited before the window"
        assert evaluation.release_allowed is False
        # HELD, never EXPIRED: READY here is what the selector downgrades to HELD for
        # this decision while storage stays READY, so the record is recoverable.
        assert evaluation.expired is False
        assert evaluation.next_lifecycle is rr.EvidenceLifecycle.READY
        assert evaluation.next_lifecycle is not rr.EvidenceLifecycle.EXPIRED


def test_held_is_recoverable_when_the_decision_returns() -> None:
    """A record withheld under one decision must RELEASE when its decision comes back."""
    assert (
        _scheduler_eval(rr.DecisionContext.COMPLETION, role_driven=True).next_lifecycle
        is rr.EvidenceLifecycle.READY
    )
    returned = _scheduler_eval(
        rr.DecisionContext.PATCH_CONSTRUCTION, role_driven=True
    )
    assert returned.release_allowed is True
    assert returned.next_lifecycle is rr.EvidenceLifecycle.RELEASED


def test_default_off_relevance_gate_dominates_the_window() -> None:
    """HONEST SCOPE. With role_driven OFF (the default), the window is UNREACHABLE.

    ``evaluate_feature_contract`` gates relevance on
    ``active.context is evidence.decision_context`` -- a strict identity on the SAME
    coordinate the window is expressed in. So every decision outside the record's own
    context is already HELD by relevance, and pass 2 (the window) is never consulted.
    The window therefore changes no byte in the default configuration; it becomes
    falsifiable only under GT_ROLE_DRIVEN_COALITION, whose role-fit fallback lets a
    record stay relevant at a decision its window has already passed.

    This test exists so that scope is recorded and cannot be quietly overclaimed.
    """
    for context in rr.DecisionContext:
        off = _scheduler_eval(context, role_driven=False)
        if context is not rr.DecisionContext.PATCH_CONSTRUCTION:
            assert off.relevant is False
            assert off.reason is (
                rr.EvidenceTransitionReason.OTHER_DECISION_CURRENTLY_ACTIVE
            )
        else:
            assert off.release_allowed is True


def test_window_rejects_unknown_and_out_of_order_boundaries() -> None:
    with pytest.raises(ValueError):
        rr.FeatureWindow(
            earliest_event="not_a_boundary",
            deliver_by="file_view",
            corrective_boundary="edit_result",
        )
    with pytest.raises(ValueError):
        rr.FeatureWindow(
            earliest_event="edit_result",
            deliver_by="file_view",
            corrective_boundary="search_result",
        )
