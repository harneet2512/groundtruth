"""C11 RED contract: commitment is per record/current decision, never global expiry."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from tests.runtime.test_wave6_lipi_red_20260725 import (
    _decision as _scheduler_decision,
    _evidence,
    _prepare,
    _runtime,
)
from tests.runtime.test_work_state_reducer import _event, _revision
from tests.runtime.test_temporal_feature_contract_runtime import (
    _context as _temporal_context,
    _decision as _temporal_decision,
    _evidence as _temporal_evidence,
)


@pytest.mark.parametrize(
    "stale_scalar",
    (
        rr.CommitmentWindowState.COMMITTED,
        rr.CommitmentWindowState.CLOSED,
    ),
)
def test_current_decision_record_ignores_stale_global_close(
    tmp_path,
    stale_scalar: rr.CommitmentWindowState,
) -> None:
    """A matching current-decision record is OPEN, not globally EXPIRED."""
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())

        plan = _prepare(runtime, commitment_window=stale_scalar)

        assert plan.delivery_attempt_id
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )
        assert all(
            transition.to_state is not rr.EvidenceLifecycle.EXPIRED
            for transition in runtime.evidence_record(
                "GT-E-caller"
            ).transition_history
        )
    finally:
        journal.close()


def test_noncurrent_record_is_held_then_released_when_its_decision_returns(
    tmp_path,
) -> None:
    """HELD is recoverable; a wrong scalar may never destroy the later release."""
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence(decision_id="patch-1"))

        other = _prepare(
            runtime,
            decision_id="patch-2",
            commitment_window=rr.CommitmentWindowState.OPEN,
        )
        assert other.delivery_attempt_id == ""
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.HELD
        )

        current = _prepare(
            runtime,
            decision_id="patch-1",
            commitment_window=rr.CommitmentWindowState.CLOSED,
            observation_id="obs-2",
            model_call_id="call-2",
        )
        assert current.delivery_attempt_id
        record = runtime.evidence_record("GT-E-caller")
        assert record.lifecycle is rr.EvidenceLifecycle.RELEASED
        assert all(
            transition.to_state is not rr.EvidenceLifecycle.EXPIRED
            for transition in record.transition_history
        )
    finally:
        journal.close()


def test_legacy_scalar_is_byte_inert_for_the_same_current_decision(
    tmp_path,
) -> None:
    open_runtime, open_journal = _runtime(tmp_path / "open")
    closed_runtime, closed_journal = _runtime(tmp_path / "closed")
    try:
        open_runtime.ingest_evidence(_evidence())
        closed_runtime.ingest_evidence(_evidence())

        open_plan = _prepare(
            open_runtime,
            commitment_window=rr.CommitmentWindowState.OPEN,
        )
        closed_plan = _prepare(
            closed_runtime,
            commitment_window=rr.CommitmentWindowState.CLOSED,
        )

        assert open_plan.delivery_attempt_id
        assert closed_plan.delivery_attempt_id
        assert (
            open_plan.compilation.capsule_text.encode("utf-8")
            == closed_plan.compilation.capsule_text.encode("utf-8")
        )
        assert (
            tuple(item.evidence_id for item in open_plan.oracle_decision.coalition)
            == tuple(
                item.evidence_id
                for item in closed_plan.oracle_decision.coalition
            )
        )
    finally:
        open_journal.close()
        closed_journal.close()


def test_scheduler_derives_window_when_legacy_scalar_is_omitted(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())

        plan = runtime.prepare_next_inference(
            decisions=(_scheduler_decision(),),
            satisfied_predicates=frozenset(
                {
                    rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
                    rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
                    rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
                    rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
                    rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
                }
            ),
            available_substrates=("graph", "lsp"),
            native_observation="native observation",
            observation_id="obs-1",
            source_model_call_id="call-0",
            model_call_id="call-1",
        )

        assert plan.delivery_attempt_id
        assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED
    finally:
        journal.close()


def test_live_scheduler_calls_do_not_supply_one_global_window_scalar() -> None:
    source_path = (
        Path(__file__).resolve().parents[2]
        / "artifact_deepswe"
        / "gt_mini_patch.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "prepare_next_inference"
    ]

    assert len(calls) == 3, "the live scheduler call inventory changed"
    assert all(
        all(keyword.arg != "commitment_window" for keyword in call.keywords)
        for call in calls
    ), "a live caller still imposes one global window state on every evidence record"
    assert "TemporalPredicate.COMMITMENT_WINDOW_OPEN" not in source, (
        "the seam still asserts the commitment predicate instead of deriving it "
        "from each record and current decision"
    )


def test_pure_evaluator_retains_explicit_per_record_expiry() -> None:
    """The safe scheduler policy must not erase the pure expiry model."""
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    evaluation = rr.evaluate_feature_contract(
        contract,
        _temporal_evidence(lifecycle=rr.EvidenceLifecycle.READY),
        _temporal_context(
            contract,
            active_decision=_temporal_decision(),
            commitment_window=rr.CommitmentWindowState.CLOSED,
        ),
    )

    assert evaluation.expired is True
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.EXPIRED
    assert (
        evaluation.reason
        is rr.EvidenceTransitionReason.DECISION_WINDOW_EXPIRED
    )


def test_work_state_window_key_tracks_decisions_not_every_observation() -> None:
    rev = _revision("repo-1")
    state = rr.WorkState.initial(attempt_id="attempt-1", revision=rev)

    first_search = _event(
        1,
        before=rev,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SEARCH_RESULT,
                subject="refreshSession",
            ),
        ),
    )
    state = rr.reduce_event(state, first_search)
    discovery_window = state.decision_window_key
    assert discovery_window == first_search.event_id

    repeated_search = _event(
        2,
        before=rev,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SEARCH_RESULT,
                subject="refreshSession",
            ),
        ),
    )
    state = rr.reduce_event(state, repeated_search)
    assert state.decision_window_key == discovery_window

    proposed = _event(
        3,
        before=rev,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.EDIT_PROPOSED,
                subject="src/auth/session.py",
            ),
        ),
    )
    state = rr.reduce_event(state, proposed)
    assert state.decision_window_key == proposed.event_id

    proposed_again = _event(
        4,
        before=rev,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.EDIT_PROPOSED,
                subject="src/auth/session.py",
            ),
        ),
    )
    state = rr.reduce_event(state, proposed_again)
    assert state.decision_window_key == proposed_again.event_id
