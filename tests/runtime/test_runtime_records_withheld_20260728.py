"""#30 step 3b — the runtime must PERSIST a holdout, on the same atomic path as every other
delivery transition.

The module-level `record_delivery_withheld` computes the new attempt; this is the journaled
side. It has to go through the same commit path as the other transitions, or a holdout would
live only in memory and vanish on replay/resume — which would make the arm unmeasurable
precisely when someone tried to measure it offline.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent
from groundtruth.runtime.reasoning_runtime import DeliveryState as DS


REVISION = rr.RevisionVector(
    repository_content="repo-withheld",
    graph="graph-withheld",
    lsp="lsp-withheld",
    runtime_evidence="runtime-withheld",
)


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "withheld.sqlite3")
    journal.open()
    return (
        rr.AttemptReasoningRuntime(
            attempt_id="attempt-withheld",
            journal=journal,
            initial_revision=REVISION,
        ),
        journal,
    )


def _seed_graph(runtime) -> None:
    action = rr.CanonicalAction(
        action_id="action-withheld",
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
        event_id="event-withheld-proposal",
        attempt_id="attempt-withheld",
        sequence=1,
        model_turn_id="call-before-withheld",
        observation_id="obs-before-withheld",
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
        event_id="event-withheld-result",
        sequence=2,
        observation_id="obs-withheld-search",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )
    runtime.append_event(proposal)
    runtime.append_event(result)


def _evidence() -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id="GT-E-withheld",
        feature_id="caller_contract",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim="Callers require the Session return contract.",
        actionable_consequence="Preserve the Session return contract.",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=("decision:patch-withheld", "hyp:refreshSession"),
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


def _plan(runtime):
    return runtime.prepare_next_inference(
        decisions=(
            rr.ActiveDecision(
                decision_id="patch-withheld",
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
        observation_id="obs-withheld",
        source_model_call_id="call-before-withheld",
        model_call_id="call-withheld",
    )


def test_runtime_persists_the_holdout_to_the_journal(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        _seed_graph(runtime)
        plan = _plan(runtime)
        assert plan.delivery_attempt_id

        withheld = runtime.record_delivery_withheld(
            plan.delivery_attempt_id, reason="shadow_holdout"
        )
        assert withheld.state is DS.WITHHELD_FOR_MEASUREMENT

        # DURABLE, not just in-memory: a holdout that vanished on replay would make the arm
        # unmeasurable exactly when someone tried to measure it offline.
        history = journal.delivery_history(plan.delivery_attempt_id)
        assert history[-1].state is DS.WITHHELD_FOR_MEASUREMENT
        assert history[-1].failure_reason == "shadow_holdout"
        # The compilation journal agrees — both sides of the atomic commit moved together.
        assert (
            journal.compilation_history(plan.delivery_attempt_id)[-1]
            .delivery_attempt.state
            is DS.WITHHELD_FOR_MEASUREMENT
        )
    finally:
        journal.close()


def test_a_dispatched_delivery_cannot_be_recorded_withheld(tmp_path) -> None:
    """Once dispatched, the bytes went out; calling it withheld would be a lie."""
    runtime, journal = _runtime(tmp_path)
    try:
        runtime.ingest_evidence(_evidence())
        _seed_graph(runtime)
        plan = _plan(runtime)
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

        with pytest.raises((ValueError, rr.StateIntegrityError)):
            runtime.record_delivery_withheld(
                plan.delivery_attempt_id, reason="too late"
            )
    finally:
        journal.close()
