"""Adversarial RED contracts found during the Wave-6 LIPI review.

These tests deliberately pin canonical-runtime invariants that the current
implementation does not yet satisfy.  They contain no production changes.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import fact_registry
from groundtruth.runtime import feature_lineage
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.miniswe_provider_boundary import (
    MiniSweProviderBoundary,
    _terminal_kind,
)


REV = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

PREDICATES = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
    }
)


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "wave6-lipi.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-lipi",
        journal=journal,
        initial_revision=REV,
    )
    runtime.reasoning_graph = replace(
        runtime.reasoning_graph,
        nodes=(
            rr.ReasoningNode(
                node_id="shared:refreshSession",
                kind=rr.ReasoningNodeKind.CANDIDATE_TARGET,
                subject="refreshSession",
            ),
        ),
    )
    return runtime, journal


def _evidence(
    *,
    evidence_id: str = "GT-E-caller",
    decision_id: str = "patch-1",
    claim: str = "Preserve the caller-visible return contract.",
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id=evidence_id,
        feature_id="caller_contract",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim=claim,
        actionable_consequence="Keep the return type stable.",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REV,
        causal_neighborhood=(
            f"decision:{decision_id}",
            "shared:refreshSession",
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
        authority=rr.Authority.RESULT_DERIVED,
    )


def _decision(decision_id: str = "patch-1") -> rr.ActiveDecision:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    return rr.ActiveDecision(
        decision_id=decision_id,
        context=contract.decision_context,
        primary_claim="Safely modify refreshSession.",
        required_roles=contract.roles,
        causal_neighborhood=("shared:refreshSession",),
        token_budget=180,
        current_revision=REV,
    )


def test_same_evidence_identity_is_attempt_owned_in_journal(tmp_path) -> None:
    journal = rr.RuntimeJournal(tmp_path / "attempt-owned-evidence.sqlite3")
    journal.open()
    try:
        evidence = _evidence(evidence_id="GT-E-shared")
        journal.append_evidence(evidence, attempt_id="attempt-A")
        journal.append_evidence(evidence, attempt_id="attempt-B")

        assert journal.evidence_records_for_attempt("attempt-A") == (evidence,)
        assert journal.evidence_records_for_attempt("attempt-B") == (evidence,)
        with pytest.raises(
            rr.StateIntegrityError,
            match="attempt-ambiguous",
        ):
            journal.evidence_history("GT-E-shared")
    finally:
        journal.close()


def _legacy_envelope(
    *,
    semantics: rr.CanonicalEvidenceSemantics,
) -> ee.EvidenceEnvelope:
    registration = fact_registry.REGISTRY["caller_contract"]
    lineage = feature_lineage.build_lineage(
        runtime_producer_id=registration.producer,
        evidence_type="caller_contract",
        actual_event=fact_registry.required_event("caller_contract") or "view",
    )
    assert lineage is not None
    return ee.EvidenceEnvelope.build(
        producer=registration.producer,
        fact_id="refreshSession",
        target="src/auth/session.py::refreshSession",
        evidence_type="caller_contract",
        payload=("legacy producer payload",),
        provenance=(("src/auth/session.py", 41),),
        confidence=0.91,
        tier=ee.VERIFIED,
        graph_revision=REV.graph,
        estimated_cost_tokens=24,
        lineage=lineage,
        producer_inputs={"symbol": "refreshSession"},
        canonical_semantics=semantics,
    )


def _canonical_semantics(
    *,
    roles: tuple[rr.EvidenceRole, ...] | None = None,
    mandatory_reason: rr.MandatoryReason | None = None,
) -> rr.CanonicalEvidenceSemantics:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    return rr.CanonicalEvidenceSemantics(
        decision_context=contract.decision_context,
        roles=roles or contract.roles,
        claim="Preserve the caller-visible return contract.",
        actionable_consequence="Keep the return type stable.",
        causal_neighborhood=(
            "decision:patch-1",
            "shared:refreshSession",
        ),
        authority=rr.Authority.RESULT_DERIVED,
        revision=REV,
        revision_dependencies=contract.revision_dependencies,
        mandatory_reason=mandatory_reason,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
    )


def _prepare(
    runtime: rr.AttemptReasoningRuntime,
    *,
    decision_id: str = "patch-1",
    commitment_window: rr.CommitmentWindowState = (
        rr.CommitmentWindowState.OPEN
    ),
    observation_id: str = "obs-1",
    model_call_id: str = "call-1",
) -> rr.InferencePlan:
    return runtime.prepare_next_inference(
        decisions=(_decision(decision_id),),
        satisfied_predicates=PREDICATES,
        commitment_window=commitment_window,
        available_substrates=("graph", "lsp"),
        native_observation="native observation",
        observation_id=observation_id,
        source_model_call_id="call-0",
        model_call_id=model_call_id,
    )


def _deliver(
    runtime: rr.AttemptReasoningRuntime,
    plan: rr.InferencePlan,
) -> rr.DeliveryAttempt:
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
        provider_response_id="resp-1",
    )
    return runtime.record_provider_terminal(
        plan.delivery_attempt_id,
        rr.ModelCallAttempt(
            model_call_id=accepted.model_call_id,
            joined_capsule_hash=accepted.joined_capsule_hash,
            provider_payload_hash=accepted.provider_payload_hash,
            provider_response_id=accepted.provider_response_id,
            terminal_kind=rr.ProviderTerminalKind.COMPLETED,
        ),
    )


@pytest.mark.parametrize(
    "window",
    [
        rr.CommitmentWindowState.COMMITTED,
        rr.CommitmentWindowState.CLOSED,
    ],
)
def test_temporal_gate_cannot_release_after_commitment_window(
    tmp_path,
    window: rr.CommitmentWindowState,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())

        plan = _prepare(runtime, commitment_window=window)

        assert plan.oracle_decision.coalition == ()
        assert plan.oracle_decision.release_allowed is False
        assert (
            plan.compilation.state
            is rr.CapsuleCompilationState.FAILED
        )
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.EXPIRED
        )
    finally:
        journal.close()


def test_temporal_gate_cannot_select_evidence_for_another_decision_id(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence(decision_id="patch-2"))

        plan = _prepare(runtime, decision_id="patch-1")

        assert plan.oracle_decision.coalition == ()
        assert plan.oracle_decision.release_allowed is False
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.HELD
        )
    finally:
        journal.close()


def test_failed_compilation_does_not_strand_evidence_as_released(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(
            _evidence(claim="Evidence\nforged reserved heading")
        )

        plan = _prepare(runtime)

        assert plan.compilation.state is rr.CapsuleCompilationState.FAILED
        assert plan.compilation.failure_code == "UNSAFE_EVIDENCE_TEXT"
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            in {rr.EvidenceLifecycle.READY, rr.EvidenceLifecycle.HELD}
        )
    finally:
        journal.close()


def test_provider_delivered_evidence_is_not_redelivered_to_same_decision(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        first = _prepare(runtime)
        assert _deliver(runtime, first).state is rr.DeliveryState.DELIVERED

        second = _prepare(
            runtime,
            observation_id="obs-2",
            model_call_id="call-2",
        )

        assert second.oracle_decision.coalition == ()
        assert second.oracle_decision.release_allowed is False
        assert (
            second.compilation.state
            is rr.CapsuleCompilationState.FAILED
        )
    finally:
        journal.close()


def test_attempt_restart_reconstructs_canonical_evidence_state(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    runtime.ingest_evidence(_evidence())
    plan = _prepare(runtime)
    assert plan.delivery_attempt_id

    restarted = rr.AttemptReasoningRuntime(
        attempt_id="attempt-lipi",
        journal=journal,
        initial_revision=REV,
    )
    try:
        assert (
            restarted.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.RELEASED
        )
        assert plan.delivery_attempt_id in restarted._compilations
    finally:
        journal.close()


def test_evidence_journal_rejects_impossible_lifecycle_jump(tmp_path) -> None:
    _, journal = _runtime(tmp_path)
    try:
        pending = _evidence()
        journal.append_evidence(pending)
        forged = replace(
            pending,
            lifecycle=rr.EvidenceLifecycle.DELIVERED,
        )

        with pytest.raises(
            rr.StateIntegrityError,
            match="lifecycle",
        ):
            journal.append_evidence(forged)
    finally:
        journal.close()


def test_delivery_journal_rejects_rejection_after_provider_acceptance(
    tmp_path,
) -> None:
    _, journal = _runtime(tmp_path)
    try:
        selected = rr.DeliveryAttempt(
            evidence_ids=("GT-E-caller",),
            capsule_hash="a" * 64,
            model_call_id="call-1",
        )
        compiled = rr.advance_delivery(
            selected,
            rr.DeliveryState.COMPILED,
            observation_id="obs-1",
        )
        joined = rr.advance_delivery(
            compiled,
            rr.DeliveryState.JOINED,
            joined_capsule_hash=selected.capsule_hash,
            provider_payload_hash="b" * 64,
        )
        dispatched = rr.advance_delivery(
            joined,
            rr.DeliveryState.DISPATCHED,
        )
        accepted = rr.advance_delivery(
            dispatched,
            rr.DeliveryState.PROVIDER_ACCEPTED,
            provider_response_id="resp-1",
        )
        for attempt in (selected, compiled, joined, dispatched, accepted):
            journal.append_delivery("delivery:call-1", attempt)
        forged_rejection = replace(
            accepted,
            state=rr.DeliveryState.PROVIDER_REJECTED,
            failure_reason="forged late rejection",
        )

        with pytest.raises(
            rr.StateIntegrityError,
            match="lifecycle|terminal",
        ):
            journal.append_delivery(
                "delivery:call-1",
                forged_rejection,
            )
    finally:
        journal.close()


def test_recovery_reconstructs_state_instead_of_only_accepting_booleans(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.work_state = replace(runtime.work_state, search_count=999)
        fault = rr.RuntimeFault(
            code=rr.FaultCode.REDUCER_INVARIANT_VIOLATION,
            component="canonical_reducer",
            signature="corrupt-work-state",
        )

        def claimed_recovery(value: rr.RecoveryInput) -> rr.RecoveryProof:
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

        recovered = runtime.handle_fault(fault, recover=claimed_recovery)

        assert recovered.health in {
            rr.RuntimeHealthState.RECOVERED,
            rr.RuntimeHealthState.QUARANTINED,
        }
        assert runtime.work_state.search_count == 0
    finally:
        journal.close()


def test_nonterminal_provider_status_never_counts_as_terminal_inference() -> None:
    response = SimpleNamespace(
        id="resp-running",
        status="in_progress",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(refusal=None),
                finish_reason="stop",
            )
        ],
    )

    assert _terminal_kind(response) is None


def test_conversion_reads_canonical_semantics_not_producer_inputs() -> None:
    semantics = _canonical_semantics()
    envelope = _legacy_envelope(semantics=semantics)

    record = rr.canonical_evidence_from_envelope(envelope)

    assert record is not None
    assert record.claim == semantics.claim
    assert record.actionable_consequence == semantics.actionable_consequence


def test_mandatory_blocker_overlay_is_representable_from_envelope() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    semantics = _canonical_semantics(
        roles=contract.roles + (rr.EvidenceRole.BLOCKER,),
        mandatory_reason=rr.MandatoryReason.BLOCKER,
    )

    record = rr.canonical_evidence_from_envelope(
        _legacy_envelope(semantics=semantics)
    )

    assert record is not None
    assert record.mandatory_reason is rr.MandatoryReason.BLOCKER
    assert rr.EvidenceRole.BLOCKER in record.roles


def test_conversion_revalidates_legacy_envelope_leak_invariants() -> None:
    envelope = _legacy_envelope(semantics=_canonical_semantics())
    tampered = replace(
        envelope,
        target="tests/auth/test_session.py::test_refresh",
        provenance=(("tests/auth/test_session.py", 9),),
    )
    assert ee.validate(tampered)

    assert rr.canonical_evidence_from_envelope(tampered) is None


def test_failed_dispatch_allows_exact_capsule_retry_on_same_observation(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        first_plan = _prepare(runtime)
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": first_plan.compilation.capsule_text,
                        }
                    ],
                }
            ]
        }
        bound = rr.bind_capsule_to_final_payload(
            first_plan.compilation,
            payload,
        )
        dispatched = rr.verify_bound_payload_at_dispatch(bound, payload)
        assert dispatched.delivery_attempt is not None
        failed = replace(
            dispatched,
            delivery_attempt=rr.record_delivery_failure(
                dispatched.delivery_attempt,
                rr.DeliveryState.DISPATCH_FAILED,
                reason="fixture transport failure",
            ),
        )

        retry = rr.compile_observation_capsule(
            native_observation="native observation",
            decision=first_plan.oracle_decision,
            observation_id=first_plan.compilation.observation_id,
            source_model_call_id="call-0",
            model_call_id="call-2",
            enabled=True,
            prior_compilations=(failed,),
        )

        assert retry.state is rr.CapsuleCompilationState.COMPILED
        assert retry.capsule_hash == first_plan.compilation.capsule_hash
    finally:
        journal.close()


def test_fallback_assurance_floor_is_enforced_during_temporal_evaluation() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    weak = replace(
        _evidence(),
        lifecycle=rr.EvidenceLifecycle.READY,
        grade=rr.EvidenceGrade.WARNING,
        authority=rr.Authority.COMMAND_FALLBACK,
    )
    context = rr.TemporalRuntimeContext(
        active_decision=_decision(),
        satisfied_predicates=PREDICATES,
        commitment_window=rr.CommitmentWindowState.OPEN,
        current_revision=REV,
        available_substrates=("exact_lexical_references",),
    )

    evaluation = rr.evaluate_feature_contract(contract, weak, context)

    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD


def test_not_open_window_keeps_evidence_ready_without_releasing_it(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())

        plan = _prepare(
            runtime,
            commitment_window=rr.CommitmentWindowState.NOT_OPEN,
        )

        assert plan.oracle_decision.coalition == ()
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.READY
        )
    finally:
        journal.close()


def test_delivered_evidence_is_not_expired_after_commitment_closes(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        first = _prepare(runtime)
        assert _deliver(runtime, first).state is rr.DeliveryState.DELIVERED

        closed = _prepare(
            runtime,
            commitment_window=rr.CommitmentWindowState.CLOSED,
            observation_id="obs-2",
            model_call_id="call-2",
        )

        assert closed.oracle_decision.coalition == ()
        assert (
            runtime.evidence_record("GT-E-caller").lifecycle
            is rr.EvidenceLifecycle.DELIVERED
        )
    finally:
        journal.close()


def test_renderer_does_not_rewrite_real_repository_identifiers_that_match_feature_id(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        evidence = replace(
            _evidence(),
            subject="src/caller_contract.py::caller_contract",
            claim="caller_contract returns the caller-visible contract.",
            provenance=("src/caller_contract.py:17",),
        )
        runtime.ingest_evidence(evidence)

        plan = _prepare(runtime)

        assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED
        assert "src/caller_contract.py:17" in plan.compilation.capsule_text
        assert (
            "caller_contract returns the caller-visible contract."
            in plan.compilation.capsule_text
        )
    finally:
        journal.close()


def test_delivery_journal_requires_compilation_join_dispatch_prefix(
    tmp_path,
) -> None:
    _, journal = _runtime(tmp_path)
    try:
        selected = rr.DeliveryAttempt(
            evidence_ids=("GT-E-caller",),
            capsule_hash="a" * 64,
            model_call_id="call-prefix",
        )
        compiled = rr.advance_delivery(
            selected,
            rr.DeliveryState.COMPILED,
            observation_id="obs-prefix",
        )
        joined = rr.advance_delivery(
            compiled,
            rr.DeliveryState.JOINED,
            joined_capsule_hash=selected.capsule_hash,
            provider_payload_hash="b" * 64,
        )
        dispatched = rr.advance_delivery(
            joined,
            rr.DeliveryState.DISPATCHED,
        )
        accepted = rr.advance_delivery(
            dispatched,
            rr.DeliveryState.PROVIDER_ACCEPTED,
            provider_response_id="resp-prefix",
        )

        with pytest.raises(
            rr.StateIntegrityError,
            match="prefix|initial|lifecycle|COMPILED",
        ):
            journal.append_delivery("delivery:prefix", accepted)
    finally:
        journal.close()


def test_provider_output_parse_failure_records_response_discarded(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        plan = _prepare(runtime)

        response = SimpleNamespace(
            id="resp-parse-failure",
            status="completed",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(refusal=None),
                    finish_reason="stop",
                )
            ],
        )

        class Model:
            def _prepare_messages_for_api(self, messages):
                return list(messages)

            def _gt_exact_provider_payload(self, messages, kwargs):
                return {"messages": messages, **kwargs}

            def _query(self, messages, **kwargs):
                return response

            def query(self, messages):
                self._query(self._prepare_messages_for_api(messages))
                raise ValueError("response action parsing failed")

        class Agent:
            def add_messages(self, *messages):
                return list(messages)

        model = Model()
        boundary = MiniSweProviderBoundary(
            model=model,
            agent=Agent(),
            attempt_runtime=runtime,
        )
        boundary.stage(
            plan.compilation,
            delivery_attempt_id=plan.delivery_attempt_id,
        )

        with pytest.raises(ValueError, match="action parsing"):
            model.query([{"role": "system", "content": "native"}])

        assert any(
            record.state is rr.DeliveryState.DELIVERED
            for record in boundary.records
        )
        assert (
            boundary.records[-1].state
            is rr.DeliveryState.RESPONSE_DISCARDED
        )
        assert (
            journal.delivery_history(plan.delivery_attempt_id)[-1].state
            is rr.DeliveryState.RESPONSE_DISCARDED
        )
    finally:
        journal.close()


def test_provider_boundary_persists_one_canonical_runtime_delivery_history(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        plan = _prepare(runtime)
        response = SimpleNamespace(
            id="resp-persisted",
            status="completed",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(refusal=None),
                    finish_reason="stop",
                )
            ],
        )

        class Model:
            def _prepare_messages_for_api(self, messages):
                return list(messages)

            def _gt_exact_provider_payload(self, messages, kwargs):
                return {"messages": messages, **kwargs}

            def _query(self, messages, **kwargs):
                return response

        class Agent:
            def add_messages(self, *messages):
                return list(messages)

        boundary = MiniSweProviderBoundary(
            model=Model(),
            agent=Agent(),
            attempt_runtime=runtime,
        )
        boundary.stage(
            plan.compilation,
            delivery_attempt_id=plan.delivery_attempt_id,
        )
        boundary.model._query(
            boundary.model._prepare_messages_for_api(
                [{"role": "system", "content": "native"}]
            )
        )

        persisted = journal.delivery_history(plan.delivery_attempt_id)
        assert boundary.records == persisted
        assert [record.state for record in persisted] == [
            rr.DeliveryState.SELECTED,
            rr.DeliveryState.COMPILED,
            rr.DeliveryState.JOINED,
            rr.DeliveryState.DISPATCHED,
            rr.DeliveryState.PROVIDER_ACCEPTED,
            rr.DeliveryState.DELIVERED,
        ]
    finally:
        journal.close()


def test_trajectory_insertion_failure_is_persisted_as_response_discarded(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        plan = _prepare(runtime)
        response = SimpleNamespace(
            id="resp-trajectory-failure",
            status="completed",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(refusal=None),
                    finish_reason="stop",
                )
            ],
        )

        class Model:
            def _prepare_messages_for_api(self, messages):
                return list(messages)

            def _gt_exact_provider_payload(self, messages, kwargs):
                return {"messages": messages, **kwargs}

            def _query(self, messages, **kwargs):
                return response

        class Agent:
            def add_messages(self, *messages):
                raise ValueError("trajectory insertion failed")

        boundary = MiniSweProviderBoundary(
            model=Model(),
            agent=Agent(),
            attempt_runtime=runtime,
        )
        boundary.stage(
            plan.compilation,
            delivery_attempt_id=plan.delivery_attempt_id,
        )
        boundary.model._query(
            boundary.model._prepare_messages_for_api(
                [{"role": "system", "content": "native"}]
            )
        )

        committed_message = {
            "role": "assistant",
            "content": "provider output",
            "extra": {
                "response": {"id": "resp-trajectory-failure"},
            },
        }
        with pytest.raises(ValueError, match="trajectory insertion"):
            boundary.agent.add_messages(committed_message)

        persisted = journal.delivery_history(plan.delivery_attempt_id)
        assert persisted[-2].state is rr.DeliveryState.DELIVERED
        assert persisted[-1].state is rr.DeliveryState.RESPONSE_DISCARDED
        assert boundary.records == persisted
    finally:
        journal.close()
