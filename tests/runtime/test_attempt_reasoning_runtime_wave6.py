"""Wave-6 RED contract for the attempt-scoped canonical runtime orchestrator.

The component APIs are already tested independently.  This file requires one
owner to compose them in causal order and persist every externally meaningful
transition.  Fixtures are local, deterministic, and provider-free.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


REV = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)
NATIVE_OBSERVATION = (
    "$ rg refreshSession src\n"
    "src/auth/session.py:41:def refreshSession(token):\n"
)


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "attempt-runtime.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-wave6",
        journal=journal,
        initial_revision=REV,
    )
    return runtime, journal


def _search_events():
    action = rr.CanonicalAction(
        action_id="action-search",
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
        event_id="ev-1-proposal",
        attempt_id="attempt-wave6",
        sequence=1,
        model_turn_id="call-10",
        observation_id="obs-9",
        revision=REV,
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
        event_id="ev-2-result",
        sequence=2,
        observation_id="obs-11",
        revision_after=REV,
        previous_event_hash=proposal.content_hash,
    )
    return proposal, result


def _decision(
    *,
    decision_id: str,
    context: rr.DecisionContext,
    roles: tuple[rr.EvidenceRole, ...],
    neighborhood: tuple[str, ...],
) -> rr.ActiveDecision:
    return rr.ActiveDecision(
        decision_id=decision_id,
        context=context,
        primary_claim=f"serve {decision_id}",
        required_roles=roles,
        causal_neighborhood=neighborhood,
        token_budget=180,
        current_revision=REV,
    )


def _evidence(
    *,
    evidence_id: str,
    feature_id: str,
    neighborhood: tuple[str, ...],
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for(feature_id)
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id=evidence_id,
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim=f"{feature_id} claim for refreshSession",
        actionable_consequence=f"apply {feature_id} before commitment",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REV,
        causal_neighborhood=neighborhood,
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
        observed_substrates=tuple(
            sorted(contract.fallback_policy.preferred_substrates)
        ),
    )


def _ready_predicates() -> frozenset[rr.TemporalPredicate]:
    return frozenset(
        {
            rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
            rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
            rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
            rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
            rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
            rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
        }
    )


def _prepare(runtime):
    patch = _decision(
        decision_id="patch-1",
        context=rr.DecisionContext.PATCH_CONSTRUCTION,
        roles=(
            rr.EvidenceRole.BEHAVIORAL_CONTRACT,
            rr.EvidenceRole.AFFECTED_CALLER,
        ),
        neighborhood=("hyp:refreshSession",),
    )
    completion = _decision(
        decision_id="completion-1",
        context=rr.DecisionContext.COMPLETION,
        roles=(rr.EvidenceRole.TERMINAL_ASSURANCE,),
        neighborhood=("hyp:refreshSession",),
    )
    return runtime.prepare_next_inference(
        decisions=(completion, patch),
        satisfied_predicates=_ready_predicates(),
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=(
            "graph",
            "lsp",
            "parser_result",
            "compiler_result",
        ),
        native_observation=NATIVE_OBSERVATION,
        observation_id="obs-12",
        source_model_call_id="call-10",
        model_call_id="call-13",
    )


def test_append_is_journal_first_then_reduces_work_and_reasoning_state(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        proposal, result = _search_events()
        runtime.append_event(proposal)
        runtime.append_event(result)

        assert journal.events("attempt-wave6") == (proposal, result)
        assert runtime.work_state.sequence == 2
        assert runtime.work_state.search_count == 1
        assert runtime.work_state.phase is rr.Phase.DISCOVERY
        assert runtime.reasoning_graph.last_source_event_sequence == 2
        assert runtime.reasoning_graph.last_source_event_hash == result.content_hash
        assert (
            runtime.reasoning_graph.node("hyp:refreshSession").hypothesis_state
            is rr.HypothesisState.CANDIDATE
        )
    finally:
        journal.close()


def test_ingest_evaluate_choose_one_decision_and_persist_held_suppression(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        for event in _search_events():
            runtime.append_event(event)

        caller = _evidence(
            evidence_id="GT-E-caller",
            feature_id="caller_contract",
            neighborhood=("decision:patch-1", "hyp:refreshSession"),
        )
        syntax = _evidence(
            evidence_id="GT-E-syntax",
            feature_id="syntax_result",
            neighborhood=("decision:propagation-1", "hyp:refreshSession"),
        )
        runtime.ingest_evidence(caller)
        runtime.ingest_evidence(syntax)

        plan = _prepare(runtime)

        assert plan.active_decision.decision_id == "patch-1"
        assert plan.oracle_decision.decision_id == "patch-1"
        assert plan.oracle_decision.release_allowed is True
        assert tuple(
            item.evidence_id for item in plan.oracle_decision.coalition
        ) == ("GT-E-caller",)
        assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED
        assert plan.compilation.decision_id == "patch-1"
        assert plan.compilation.capsule_text.count("Decision\n") == 1
        assert tuple(
            item.state
            for item in journal.delivery_history(plan.delivery_attempt_id)
        ) == (
            rr.DeliveryState.SELECTED,
            rr.DeliveryState.COMPILED,
        )
        assert plan.held_evidence_ids == ("GT-E-syntax",)
        assert plan.suppressed_decision_ids == ("completion-1",)

        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )
        assert (
            runtime.evidence_record("GT-E-syntax").lifecycle
            is rr.EvidenceLifecycle.HELD
        )
        assert (
            journal.evidence_history("GT-E-syntax")[-1].lifecycle
            is rr.EvidenceLifecycle.HELD
        )
        persisted_oracle = journal.oracle_history("attempt-wave6")[-1]
        assert persisted_oracle.decision_id == "patch-1"
        assert any(
            row.evidence_id == "GT-E-syntax"
            and row.reason is rr.SuppressionReason.OTHER_DECISION
            for row in persisted_oracle.suppressed
        )
    finally:
        journal.close()


def test_provider_callbacks_mark_evidence_delivered_only_at_terminal_inference(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        for event in _search_events():
            runtime.append_event(event)
        runtime.ingest_evidence(
            _evidence(
                evidence_id="GT-E-caller",
                feature_id="caller_contract",
                neighborhood=("decision:patch-1", "hyp:refreshSession"),
            )
        )
        plan = _prepare(runtime)
        payload = {
            "model": "local/provider-fixture",
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
            ],
        }

        joined = runtime.bind_provider_payload(
            plan.delivery_attempt_id,
            payload,
        )
        dispatched = runtime.mark_dispatched(plan.delivery_attempt_id, payload)
        accepted = runtime.mark_provider_accepted(
            plan.delivery_attempt_id,
            provider_response_id="resp-13",
        )

        assert joined.state is rr.DeliveryState.JOINED
        assert dispatched.state is rr.DeliveryState.DISPATCHED
        assert accepted.state is rr.DeliveryState.PROVIDER_ACCEPTED
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )

        delivered = runtime.record_provider_terminal(
            plan.delivery_attempt_id,
            rr.ModelCallAttempt(
                model_call_id="call-13",
                joined_capsule_hash=accepted.joined_capsule_hash,
                provider_payload_hash=accepted.provider_payload_hash,
                provider_response_id="resp-13",
                terminal_kind=rr.ProviderTerminalKind.COMPLETED,
            ),
        )

        assert delivered.state is rr.DeliveryState.DELIVERED
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.DELIVERED
        )
        assert [
            item.state
            for item in journal.delivery_history(plan.delivery_attempt_id)
        ] == [
            rr.DeliveryState.SELECTED,
            rr.DeliveryState.COMPILED,
            rr.DeliveryState.JOINED,
            rr.DeliveryState.DISPATCHED,
            rr.DeliveryState.PROVIDER_ACCEPTED,
            rr.DeliveryState.DELIVERED,
        ]
    finally:
        journal.close()


def test_core_fault_recovers_once_then_quarantines_and_preserves_native_path(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        for event in _search_events():
            runtime.append_event(event)

        request = runtime.recovery_input()
        fault = rr.RuntimeFault(
            code=rr.FaultCode.NONDETERMINISTIC_REPLAY,
            component="canonical_reducer",
            signature="nondeterministic-replay:ev-2",
            event_id="ev-2-result",
        )

        def valid_recovery(value: rr.RecoveryInput) -> rr.RecoveryProof:
            return rr.RecoveryProof(
                snapshot_id=value.snapshot_id,
                snapshot_state_hash=value.snapshot_state_hash,
                committed_event_ids=value.committed_event_ids,
                committed_tail_hash=value.committed_tail_hash,
                snapshot_hash_valid=True,
                event_sequence_complete=True,
                deterministic_replay=True,
                state_hash_matches=True,
                reasoning_graph_hash_matches=True,
                evidence_graph_hash_matches=True,
                repository_revision_consistent=True,
                invariants_pass=True,
                recovered_state_hash=value.snapshot_state_hash,
            )

        recovered = runtime.handle_fault(fault, recover=valid_recovery)
        assert recovered.health is rr.RuntimeHealthState.RECOVERED
        assert recovered.assurance is rr.AssuranceStatus.ASSURED
        assert recovered.native_path_enabled is True

        quarantined = runtime.handle_fault(
            fault,
            recover=lambda _request: (_ for _ in ()).throw(
                AssertionError("the same fault signature must not retry")
            ),
        )
        assert quarantined.health is rr.RuntimeHealthState.QUARANTINED
        assert quarantined.assurance is rr.AssuranceStatus.UNASSURED
        assert quarantined.gt_emission_enabled is False
        assert quarantined.gt_interruption_enabled is False
        assert quarantined.gt_certification_enabled is False
        assert quarantined.native_path_enabled is True

        fallback = _prepare(runtime)
        assert fallback.native_observation.encode("utf-8") == (
            NATIVE_OBSERVATION.encode("utf-8")
        )
        assert (
            fallback.compilation.state
            is rr.CapsuleCompilationState.DISABLED
        )
        assert fallback.oracle_decision.coalition == ()
        assert fallback.assurance is rr.AssuranceStatus.UNASSURED
    finally:
        journal.close()


def test_restart_reconstructs_terminal_delivery_and_degraded_health(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        for event in _search_events():
            runtime.append_event(event)
        runtime.ingest_evidence(
            _evidence(
                evidence_id="GT-E-caller",
                feature_id="caller_contract",
                neighborhood=("decision:patch-1", "hyp:refreshSession"),
            )
        )
        plan = _prepare(runtime)
        payload = {
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
        runtime.bind_provider_payload(plan.delivery_attempt_id, payload)
        runtime.mark_dispatched(plan.delivery_attempt_id, payload)
        accepted = runtime.mark_provider_accepted(
            plan.delivery_attempt_id,
            provider_response_id="resp-restart",
        )
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
        runtime.handle_fault(
            rr.RuntimeFault(
                code=rr.FaultCode.EVIDENCE_PRODUCER_FAILED,
                component="history",
                signature="history@restart",
            ),
            recover=lambda _request: (_ for _ in ()).throw(
                AssertionError("component faults must not run core recovery")
            ),
        )

        restarted = rr.AttemptReasoningRuntime(
            attempt_id="attempt-wave6",
            journal=journal,
            initial_revision=REV,
        )

        assert (
            restarted.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.DELIVERED
        )
        assert (
            restarted._compilations[
                plan.delivery_attempt_id
            ].delivery_attempt.state
            is rr.DeliveryState.DELIVERED
        )
        assert (
            restarted.failure_state.health
            is rr.RuntimeHealthState.DEGRADED
        )
        assert restarted.failure_state.isolated_components == ("history",)
    finally:
        journal.close()


def test_restart_preserves_attempt_quarantine_as_terminal_health(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        fault = rr.RuntimeFault(
            code=rr.FaultCode.STATE_HASH_MISMATCH,
            component="canonical_runtime",
            signature="state-hash@restart",
        )
        quarantined = runtime.handle_fault(
            fault,
            recover=lambda request: rr.RecoveryProof(
                snapshot_id=request.snapshot_id,
                snapshot_state_hash=request.snapshot_state_hash,
                committed_event_ids=request.committed_event_ids,
                committed_tail_hash=request.committed_tail_hash,
                snapshot_hash_valid=True,
                event_sequence_complete=True,
                deterministic_replay=False,
                state_hash_matches=False,
                reasoning_graph_hash_matches=True,
                evidence_graph_hash_matches=True,
                repository_revision_consistent=True,
                invariants_pass=False,
                recovered_state_hash="",
            ),
        )
        assert quarantined.health is rr.RuntimeHealthState.QUARANTINED

        restarted = rr.AttemptReasoningRuntime(
            attempt_id="attempt-wave6",
            journal=journal,
            initial_revision=REV,
        )

        assert (
            restarted.failure_state.health
            is rr.RuntimeHealthState.QUARANTINED
        )
        assert restarted.failure_state.gt_emission_enabled is False
        assert restarted.failure_state.native_path_enabled is True
    finally:
        journal.close()
