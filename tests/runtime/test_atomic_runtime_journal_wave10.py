"""RED contracts for atomic delivery/compilation/evidence journal transitions."""

from __future__ import annotations

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


REVISION = rr.RevisionVector(
    repository_content="repo-wave10",
    graph="graph-wave10",
    lsp="lsp-wave10",
    runtime_evidence="runtime-wave10",
)


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "atomic-runtime.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-wave10",
        journal=journal,
        initial_revision=REVISION,
    )
    return runtime, journal


def _seed_reasoning_graph(runtime: rr.AttemptReasoningRuntime) -> None:
    action = rr.CanonicalAction(
        action_id="action-wave10",
        operation=rr.ActionOperation.SEARCH,
        tool_family="search",
        tool_name="mini-swe",
        structured_operation="search",
        subject="refreshSession",
        query="refreshSession",
        targets=("src",),
        raw_command="opaque structured search",
    )
    proposal = miniswe.canonicalize_action_proposal(
        action,
        event_id="event-wave10-proposal",
        attempt_id="attempt-wave10",
        sequence=1,
        model_turn_id="call-before-wave10",
        observation_id="obs-before-wave10",
        revision=REVISION,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="other",
            carrier_kind="other",
            command="carrier is audit-only",
            output="src/auth/session.py:41:def refreshSession(token):",
            exit_status=0,
            semantic_events=(),
            semantics_authoritative=True,
        ),
        proposal=proposal,
        result=rr.CanonicalResult(
            status="success",
            exit_code=0,
            hit_count=1,
            files_hit=("src/auth/session.py",),
        ),
        event_id="event-wave10-result",
        sequence=2,
        observation_id="obs-wave10-search",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )
    runtime.append_event(proposal)
    runtime.append_event(result)


def _evidence() -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id="GT-E-wave10",
        feature_id="caller_contract",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim="Callers require the Session return contract.",
        actionable_consequence="Preserve the Session return contract.",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            "decision:patch-wave10",
            "hyp:refreshSession",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.STRUCTURED,
    )


def _prepare(runtime: rr.AttemptReasoningRuntime) -> rr.InferencePlan:
    return runtime.prepare_next_inference(
        decisions=(
            rr.ActiveDecision(
                decision_id="patch-wave10",
                context=rr.DecisionContext.PATCH_CONSTRUCTION,
                primary_claim="Safely patch refreshSession.",
                required_roles=(
                    rr.EvidenceRole.BEHAVIORAL_CONTRACT,
                    rr.EvidenceRole.AFFECTED_CALLER,
                ),
                causal_neighborhood=("hyp:refreshSession",),
                token_budget=180,
                current_revision=REVISION,
            ),
        ),
        satisfied_predicates=frozenset(
            {
                rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
                rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
                rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
                rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
                rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
                rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
            }
        ),
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=("graph", "lsp"),
        native_observation="$ sed -n '41,70p' src/auth/session.py\n",
        observation_id="obs-wave10",
        source_model_call_id="call-before-wave10",
        model_call_id="call-wave10",
    )


def _payload(plan: rr.InferencePlan) -> dict[str, object]:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": plan.compilation.capsule_text,
                    }
                ],
            }
        ]
    }


def test_prepare_failure_rolls_back_delivery_compilation_and_release_memory(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        _seed_reasoning_graph(runtime)
        journal.connection.execute(
            """
            CREATE TEMP TRIGGER fail_initial_compilation
            BEFORE INSERT ON compilation_journal
            BEGIN
                SELECT RAISE(ABORT, 'injected initial compilation failure');
            END
            """
        )

        with pytest.raises(
            rr.StateIntegrityError,
            match="compilation|atomic|integrity",
        ):
            _prepare(runtime)

        delivery_attempt_id = "delivery:call-wave10"
        assert journal.delivery_history(delivery_attempt_id) == ()
        assert journal.compilation_history(delivery_attempt_id) == ()
        assert journal.delivery_attempt_ids_for_attempt("attempt-wave10") == ()
        assert runtime._compilations == {}
        assert runtime._delivery_attempt_ids == {}
        assert (
            runtime.evidence_record("GT-E-wave10").lifecycle
            is not rr.EvidenceLifecycle.RELEASED
        )
        assert (
            journal.evidence_history(
                "GT-E-wave10",
                attempt_id="attempt-wave10",
            )[-1].lifecycle
            is runtime.evidence_record("GT-E-wave10").lifecycle
        )
    finally:
        journal.close()


def test_provider_transition_failure_rolls_back_both_journals_and_memory(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        _seed_reasoning_graph(runtime)
        plan = _prepare(runtime)
        before = runtime._compilations[plan.delivery_attempt_id]
        journal.connection.execute(
            """
            CREATE TEMP TRIGGER fail_joined_compilation
            BEFORE INSERT ON compilation_journal
            WHEN NEW.journal_sequence = 2
            BEGIN
                SELECT RAISE(ABORT, 'injected joined compilation failure');
            END
            """
        )

        with pytest.raises(
            rr.StateIntegrityError,
            match="compilation|atomic|integrity",
        ):
            runtime.bind_provider_payload(
                plan.delivery_attempt_id,
                _payload(plan),
            )

        assert tuple(
            item.state
            for item in journal.delivery_history(plan.delivery_attempt_id)
        ) == (
            rr.DeliveryState.SELECTED,
            rr.DeliveryState.COMPILED,
        )
        assert len(journal.compilation_history(plan.delivery_attempt_id)) == 1
        assert runtime._compilations[plan.delivery_attempt_id] == before
        assert (
            runtime._compilations[
                plan.delivery_attempt_id
            ].delivery_attempt.state
            is rr.DeliveryState.COMPILED
        )
    finally:
        journal.close()


def test_terminal_failure_rolls_back_delivery_and_evidence_lifecycle(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        _seed_reasoning_graph(runtime)
        plan = _prepare(runtime)
        payload = _payload(plan)
        runtime.bind_provider_payload(plan.delivery_attempt_id, payload)
        runtime.mark_dispatched(plan.delivery_attempt_id, payload)
        accepted = runtime.mark_provider_accepted(
            plan.delivery_attempt_id,
            provider_response_id="resp-wave10",
        )
        before = runtime._compilations[plan.delivery_attempt_id]
        journal.connection.execute(
            """
            CREATE TEMP TRIGGER fail_delivered_evidence
            BEFORE INSERT ON evidence_attempt_journal
            WHEN NEW.lifecycle = 'DELIVERED'
            BEGIN
                SELECT RAISE(ABORT, 'injected delivered evidence failure');
            END
            """
        )

        with pytest.raises(
            rr.StateIntegrityError,
            match="evidence|atomic|integrity",
        ):
            runtime.record_provider_terminal(
                plan.delivery_attempt_id,
                rr.ModelCallAttempt(
                    model_call_id=accepted.model_call_id,
                    joined_capsule_hash=accepted.joined_capsule_hash,
                    provider_payload_hash=accepted.provider_payload_hash,
                    provider_response_id=accepted.provider_response_id,
                    terminal_kind=rr.ProviderTerminalKind.COMPLETED,
                ),
            )

        assert (
            journal.delivery_history(plan.delivery_attempt_id)[-1].state
            is rr.DeliveryState.PROVIDER_ACCEPTED
        )
        assert (
            journal.compilation_history(
                plan.delivery_attempt_id
            )[-1].delivery_attempt.state
            is rr.DeliveryState.PROVIDER_ACCEPTED
        )
        assert runtime._compilations[plan.delivery_attempt_id] == before
        assert (
            runtime.evidence_record("GT-E-wave10").lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )
        assert (
            journal.evidence_history(
                "GT-E-wave10",
                attempt_id="attempt-wave10",
            )[-1].lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )
    finally:
        journal.close()


def test_join_failure_is_a_reasoned_atomic_terminal_state(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        _seed_reasoning_graph(runtime)
        plan = _prepare(runtime)

        failed = runtime.record_delivery_failure(
            plan.delivery_attempt_id,
            rr.DeliveryState.JOIN_FAILED,
            reason="capsule absent from final structural payload",
        )

        assert failed.state is rr.DeliveryState.JOIN_FAILED
        assert failed.failure_reason == (
            "capsule absent from final structural payload"
        )
        assert (
            journal.delivery_history(plan.delivery_attempt_id)[-1]
            == failed
        )
        assert (
            journal.compilation_history(
                plan.delivery_attempt_id
            )[-1].delivery_attempt
            == failed
        )
        assert (
            runtime.evidence_record("GT-E-wave10").lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )
    finally:
        journal.close()
