"""RED contracts for revision-bound evidence generations and re-ingestion.

One physical fact may be offered repeatedly during an attempt.  Re-offering the
same computation at the same revision must not reset its canonical lifecycle,
while a recomputation at a different revision must be a distinct generation.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.canonical_producers import (
    ProducerContext,
    produce_syntax_result,
)


REV = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="edit-1",
)
REV_2 = replace(REV, runtime_evidence="edit-2")


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "generation-reingest.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-generation",
        journal=journal,
        initial_revision=REV,
    )
    runtime.reasoning_graph = replace(
        runtime.reasoning_graph,
        nodes=(
            rr.ReasoningNode(
                node_id="path:src/pkg/module.py",
                kind=rr.ReasoningNodeKind.CANDIDATE_TARGET,
                subject="src/pkg/module.py",
            ),
        ),
    )
    return runtime, journal


def _record(
    *,
    evidence_id: str = "GT-E-generation",
    owner_feature_ids: tuple[str, ...] = (),
    revision: rr.RevisionVector = REV,
    claim: str = "The current edit has invalid syntax.",
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("syntax_result")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id=evidence_id,
        feature_id="syntax_result",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="src/pkg/module.py",
        claim=claim,
        actionable_consequence="Repair the syntax before propagating the patch.",
        provenance=("src/pkg/module.py:9",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=revision,
        causal_neighborhood=("decision:patch-propagation", "path:src/pkg/module.py"),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=rr.MandatoryReason.BLOCKER,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.RESULT_DERIVED,
        owner_feature_ids=owner_feature_ids,
    )


def _released(record: rr.EvidenceRecord) -> rr.EvidenceRecord:
    ready = rr.transition_evidence(
        record,
        rr.EvidenceLifecycle.READY,
        reason_code=rr.EvidenceTransitionReason.READINESS_RULES_SATISFIED,
    )
    return rr.transition_evidence(
        ready,
        rr.EvidenceLifecycle.RELEASED,
        reason_code=rr.EvidenceTransitionReason.DECISION_WINDOW_OPEN,
    )


def _persist_released(
    runtime: rr.AttemptReasoningRuntime,
    record: rr.EvidenceRecord,
) -> rr.EvidenceRecord:
    ready = rr.transition_evidence(
        record,
        rr.EvidenceLifecycle.READY,
        reason_code=rr.EvidenceTransitionReason.READINESS_RULES_SATISFIED,
    )
    runtime._persist_evidence(ready)
    released = rr.transition_evidence(
        ready,
        rr.EvidenceLifecycle.RELEASED,
        reason_code=rr.EvidenceTransitionReason.DECISION_WINDOW_OPEN,
    )
    runtime._persist_evidence(released)
    return released


def _syntax_envelope(revision: rr.RevisionVector):
    return produce_syntax_result(
        context=ProducerContext(
            subject="src/pkg/module.py",
            provenance=(("src/pkg/module.py", 9),),
            revision=revision,
            decision_id="patch-propagation",
            causal_neighborhood=(
                "decision:patch-propagation",
                "path:src/pkg/module.py",
            ),
        ),
        result={
            "verdict": "syntax_error",
            "diagnostic": "src/pkg/module.py:9: SyntaxError: invalid syntax",
            "language": ".py",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
    )


def _decision() -> rr.ActiveDecision:
    contract = rr.feature_contract_for("syntax_result")
    assert contract is not None
    return rr.ActiveDecision(
        decision_id="patch-propagation",
        context=contract.decision_context,
        primary_claim="Repair the invalid edit.",
        required_roles=contract.roles,
        causal_neighborhood=("path:src/pkg/module.py",),
        token_budget=180,
        current_revision=REV,
    )


def _predicates() -> frozenset[rr.TemporalPredicate]:
    return frozenset(
        {
            rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
            rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
            rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
            rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
            rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
            rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
            rr.TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,
        }
    )


def test_same_generation_reingestion_preserves_lifecycle_and_unions_owner(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        initial = _record()
        runtime.ingest_evidence(initial)
        _persist_released(runtime, initial)

        runtime.ingest_evidence(
            _record(owner_feature_ids=("GT_EDIT_CHECK",))
        )

        current = runtime.evidence_record(initial.evidence_id)
        assert current.lifecycle is rr.EvidenceLifecycle.RELEASED
        assert len(current.transition_history) == 2
        assert current.owner_feature_ids == ("GT_EDIT_CHECK",)
        assert journal.evidence_history(
            initial.evidence_id,
            attempt_id=runtime.attempt_id,
        )[-1] == current

        # An owner-less duplicate cannot erase audit lineage.
        runtime.ingest_evidence(initial)
        assert runtime.evidence_record(initial.evidence_id) == current

        restarted = rr.AttemptReasoningRuntime(
            attempt_id=runtime.attempt_id,
            journal=journal,
            initial_revision=REV,
        )
        assert restarted.evidence_record(initial.evidence_id) == current
    finally:
        journal.close()


def test_same_generation_conflicting_semantics_is_core_corruption(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        initial = _record()
        runtime.ingest_evidence(initial)
        released = _persist_released(runtime, initial)

        with pytest.raises(
            rr.StateIntegrityError,
            match="different canonical generation",
        ):
            runtime.ingest_evidence(
                _record(claim="A conflicting claim under the same generation ID.")
            )

        assert runtime.evidence_record(initial.evidence_id) == released
    finally:
        journal.close()


def test_owner_union_rejects_capability_not_bound_to_the_fact(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        with pytest.raises(rr.StateIntegrityError, match="byte owner"):
            runtime.ingest_evidence(
                _record(owner_feature_ids=("GT_PATCH_DELTA",))
            )
        assert runtime._evidence == {}
    finally:
        journal.close()


def test_revision_recomputation_gets_a_distinct_generation_identity(
    tmp_path,
) -> None:
    first = rr.canonical_evidence_from_envelope(_syntax_envelope(REV))
    first_repeat = rr.canonical_evidence_from_envelope(_syntax_envelope(REV))
    second = rr.canonical_evidence_from_envelope(_syntax_envelope(REV_2))

    assert first is not None
    assert first_repeat is not None
    assert second is not None
    assert first_repeat == first
    assert first.revision != second.revision
    assert first.evidence_id != second.evidence_id

    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(first)
        runtime.ingest_evidence(second)
        assert runtime.evidence_record(first.evidence_id) == first
        assert runtime.evidence_record(second.evidence_id) == second
        assert len(runtime._evidence) == 2
    finally:
        journal.close()


def test_response_commit_atomically_activates_delivered_evidence(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        record = _record(owner_feature_ids=("GT_EDIT_CHECK",))
        runtime.ingest_evidence(record)
        plan = runtime.prepare_next_inference(
            decisions=(_decision(),),
            satisfied_predicates=_predicates(),
            commitment_window=rr.CommitmentWindowState.OPEN,
            available_substrates=("parser_result",),
            native_observation="native observation",
            observation_id="obs-1",
            source_model_call_id="call-0",
            model_call_id="call-1",
        )
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": plan.compilation.capsule_text}
                    ],
                }
            ]
        }
        runtime.bind_provider_payload(plan.delivery_attempt_id, payload)
        runtime.mark_dispatched(plan.delivery_attempt_id, payload)
        accepted = runtime.mark_provider_accepted(
            plan.delivery_attempt_id,
            provider_response_id="response-1",
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
        assert (
            runtime.evidence_record(record.evidence_id).lifecycle
            is rr.EvidenceLifecycle.DELIVERED
        )

        committed = runtime.commit_provider_response(
            plan.delivery_attempt_id,
            response_hash="f" * 64,
        )

        assert committed.state is rr.DeliveryState.RESPONSE_COMMITTED
        assert (
            runtime.evidence_record(record.evidence_id).lifecycle
            is rr.EvidenceLifecycle.ACTIVE
        )
        assert (
            journal.evidence_history(
                record.evidence_id,
                attempt_id=runtime.attempt_id,
            )[-1].lifecycle
            is rr.EvidenceLifecycle.ACTIVE
        )
    finally:
        journal.close()
