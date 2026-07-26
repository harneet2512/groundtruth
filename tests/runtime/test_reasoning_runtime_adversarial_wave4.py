"""Wave-4 RED contracts for the canonical reasoning and coalition boundary.

These tests pin the adversarial cases that schema-only tests miss:

* reasoning signals are projections of committed canonical events, not an
  independently trusted input stream;
* one compound event may produce several ordered operational signals;
* event/hash/revision/authority binding is checked at the reducer boundary;
* coalition connectivity is a real reasoning-graph path, not label overlap;
* distinct actionable consequences may share one coverage role; and
* visibility and oracle identity are scoped to the exact open decision.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REV_1 = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)
REV_2 = rr.RevisionVector(
    repository_content="repo-2",
    graph="graph-2",
    lsp="lsp-2",
    runtime_evidence="runtime-2",
)


def _canonical_event(
    *,
    event_id: str,
    sequence: int,
    before: rr.RevisionVector = REV_1,
    after: rr.RevisionVector = REV_1,
    previous_event_hash: str = "",
    authority: rr.Authority = rr.Authority.STRUCTURED,
    outcomes: tuple[rr.SemanticOutcome, ...],
) -> rr.CanonicalEvent:
    return rr.CanonicalEvent(
        event_id=event_id,
        attempt_id="attempt-wave4",
        sequence=sequence,
        kind=rr.EventKind.OBSERVATION_COMMITTED,
        authority=authority,
        outcomes=outcomes,
        revision_before=before,
        revision_after=after,
        previous_event_hash=previous_event_hash,
        action_id=f"action-{sequence}",
        model_turn_id=f"call-{sequence}",
        observation_id=f"obs-{sequence}",
    )


def _compound_discovery_event() -> rr.CanonicalEvent:
    return _canonical_event(
        event_id="ev-1",
        sequence=1,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SEARCH_RESULT,
                subject="refreshSession",
                authority=rr.Authority.STRUCTURED,
                provenance=("structured_search_result",),
            ),
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SYMBOL_VIEWED,
                subject="refreshSession",
                authority=rr.Authority.STRUCTURED,
                provenance=("structured_symbol_view",),
            ),
        ),
    )


def _reasoning_graph(*, connected: bool) -> rr.ReasoningGraph:
    nodes = (
        rr.ReasoningNode(
            node_id="decision:patch",
            kind=rr.ReasoningNodeKind.DECISION,
            subject="patch refreshSession",
        ),
        rr.ReasoningNode(
            node_id="symbol:refreshSession",
            kind=rr.ReasoningNodeKind.CANDIDATE_TARGET,
            subject="refreshSession",
        ),
        rr.ReasoningNode(
            node_id="contract:refreshSession",
            kind=rr.ReasoningNodeKind.CONTRACT,
            subject="Session return contract",
        ),
        rr.ReasoningNode(
            node_id="caller:middleware",
            kind=rr.ReasoningNodeKind.CONTRACT,
            subject="middleware caller",
        ),
        rr.ReasoningNode(
            node_id="caller:route",
            kind=rr.ReasoningNodeKind.CONTRACT,
            subject="route caller",
        ),
    )
    edges: tuple[rr.ReasoningEdge, ...] = ()
    if connected:
        edges = (
            rr.ReasoningEdge(
                source_id="decision:patch",
                target_id="symbol:refreshSession",
                kind=rr.ReasoningEdgeKind.TARGETS,
                event_id="ev-graph",
            ),
            rr.ReasoningEdge(
                source_id="symbol:refreshSession",
                target_id="contract:refreshSession",
                kind=rr.ReasoningEdgeKind.REQUIRES,
                event_id="ev-graph",
            ),
            rr.ReasoningEdge(
                source_id="contract:refreshSession",
                target_id="caller:middleware",
                kind=rr.ReasoningEdgeKind.DEPENDS_ON,
                event_id="ev-graph",
            ),
            rr.ReasoningEdge(
                source_id="contract:refreshSession",
                target_id="caller:route",
                kind=rr.ReasoningEdgeKind.DEPENDS_ON,
                event_id="ev-graph",
            ),
        )
    return rr.ReasoningGraph(
        attempt_id="attempt-wave4",
        revision=REV_1,
        nodes=nodes,
        edges=edges,
    )


def _decision(
    *,
    decision_id: str = "decision-open",
    required_roles: tuple[rr.EvidenceRole, ...] = (
        rr.EvidenceRole.TARGET_IDENTITY,
        rr.EvidenceRole.BEHAVIORAL_CONTRACT,
    ),
) -> rr.ActiveDecision:
    return rr.ActiveDecision(
        decision_id=decision_id,
        context=rr.DecisionContext.PATCH_CONSTRUCTION,
        primary_claim="repair refreshSession without breaking its return contract",
        required_roles=required_roles,
        causal_neighborhood=("decision:patch",),
        token_budget=120,
        current_revision=REV_1,
    )


def _evidence(
    evidence_id: str,
    *,
    role: rr.EvidenceRole,
    graph_node: str,
    subject: str,
    consequence: str,
    visible_to_decision_ids: tuple[str, ...] = (),
) -> rr.EvidenceRecord:
    values = dict(
        evidence_id=evidence_id,
        # This test isolates graph/coalition semantics.  A synthetic producer
        # avoids coupling TARGET_IDENTITY probes to caller_contract's stricter
        # canonical feature-role contract, which has its own adversarial tests.
        feature_id="wave4_graph_probe",
        decision_context=rr.DecisionContext.PATCH_CONSTRUCTION,
        roles=(role,),
        subject=subject,
        claim=f"{subject} is relevant to the open patch decision",
        actionable_consequence=consequence,
        provenance=(f"source:{subject}",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REV_1,
        causal_neighborhood=(graph_node,),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=12,
        failure_prevention=4,
        causal_value=4,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=("repository_content", "graph"),
    )
    if visible_to_decision_ids:
        values["visible_to_decision_ids"] = visible_to_decision_ids
    return rr.EvidenceRecord(**values)


def test_compound_stored_event_derives_multiple_bound_signals(tmp_path) -> None:
    event = _compound_discovery_event()
    with rr.EventStore(tmp_path / "events.sqlite3") as store:
        store.append(event)
        stored = store.events("attempt-wave4")

    assert stored == (event,)
    signals = rr.derive_operational_signals(stored[0], starting_sequence=1)

    assert [signal.kind for signal in signals] == [
        rr.OperationalSignalKind.EXACT_SEARCH,
        rr.OperationalSignalKind.FOCUSED_SYMBOL_VIEW,
    ]
    assert [signal.sequence for signal in signals] == [1, 2]
    assert all(signal.source_event_sequence == event.sequence for signal in signals)
    assert all(signal.source_event_hash == event.content_hash for signal in signals)
    assert all(signal.revision == event.revision_after for signal in signals)
    assert all(signal.authority is rr.Authority.STRUCTURED for signal in signals)

    reduced = rr.reduce_reasoning_event(
        rr.ReasoningGraph.initial(
            attempt_id="attempt-wave4",
            revision=REV_1,
        ),
        event=stored[0],
        signals=signals,
    )
    assert reduced.sequence == 2
    assert reduced.last_source_event_sequence == 1
    assert reduced.last_source_event_hash == event.content_hash
    assert reduced.source_event_ids == ("ev-1",)
    assert (
        reduced.node("hyp:refreshSession").hypothesis_state
        is rr.HypothesisState.ACTIVE
    )


def test_reasoning_event_rejects_hash_revision_authority_and_event_gaps() -> None:
    event = _compound_discovery_event()
    signals = rr.derive_operational_signals(event, starting_sequence=1)
    initial = rr.ReasoningGraph.initial(
        attempt_id="attempt-wave4",
        revision=REV_1,
    )

    with pytest.raises(rr.StateIntegrityError, match="hash"):
        rr.reduce_reasoning_event(
            initial,
            event=event,
            signals=(replace(signals[0], source_event_hash="f" * 64),),
        )

    with pytest.raises(rr.StateIntegrityError, match="revision"):
        rr.reduce_reasoning_event(
            initial,
            event=event,
            signals=(replace(signals[0], revision=REV_2),),
        )

    low_authority_event = _canonical_event(
        event_id="ev-low",
        sequence=1,
        authority=rr.Authority.COMMAND_FALLBACK,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SEARCH_RESULT,
                subject="refreshSession",
                authority=rr.Authority.COMMAND_FALLBACK,
            ),
        ),
    )
    low_signal = rr.derive_operational_signals(
        low_authority_event,
        starting_sequence=1,
    )[0]
    with pytest.raises(rr.StateIntegrityError, match="authority"):
        rr.reduce_reasoning_event(
            initial,
            event=low_authority_event,
            signals=(replace(low_signal, authority=rr.Authority.STRUCTURED),),
        )

    event_gap = _canonical_event(
        event_id="ev-3",
        sequence=3,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SEARCH_RESULT,
                subject="refreshSession",
                authority=rr.Authority.STRUCTURED,
            ),
        ),
    )
    gap_signal = rr.derive_operational_signals(
        event_gap,
        starting_sequence=1,
    )
    with pytest.raises(rr.StateIntegrityError, match="sequence|gap"):
        rr.reduce_reasoning_event(
            initial,
            event=event_gap,
            signals=gap_signal,
        )


def test_coalition_requires_a_real_reasoning_graph_path() -> None:
    target = _evidence(
        "target",
        role=rr.EvidenceRole.TARGET_IDENTITY,
        graph_node="symbol:refreshSession",
        subject="refreshSession",
        consequence="edit refreshSession",
    )
    contract = _evidence(
        "contract",
        role=rr.EvidenceRole.BEHAVIORAL_CONTRACT,
        graph_node="contract:refreshSession",
        subject="Session return contract",
        consequence="preserve the Session return value",
    )

    connected = rr.select_evidence_coalition(
        _decision(),
        (target, contract),
        reasoning_graph=_reasoning_graph(connected=True),
    )
    assert connected.release_allowed is True
    assert {item.evidence_id for item in connected.coalition} == {
        "target",
        "contract",
    }

    disconnected = rr.select_evidence_coalition(
        _decision(),
        (target, contract),
        reasoning_graph=_reasoning_graph(connected=False),
    )
    assert disconnected.release_allowed is False
    assert disconnected.coalition == ()
    assert {
        row.reason for row in disconnected.suppressed
    } == {rr.SuppressionReason.DISCONNECTED}


def test_same_role_items_survive_when_each_has_a_unique_actionable_consequence() -> None:
    middleware = _evidence(
        "caller-middleware",
        role=rr.EvidenceRole.AFFECTED_CALLER,
        graph_node="caller:middleware",
        subject="auth.middleware.refresh_request",
        consequence="update the middleware call to pass the new argument",
    )
    route = _evidence(
        "caller-route",
        role=rr.EvidenceRole.AFFECTED_CALLER,
        graph_node="caller:route",
        subject="auth.routes.rotate_token",
        consequence="update the route call to preserve the Session result",
    )

    result = rr.select_evidence_coalition(
        _decision(required_roles=(rr.EvidenceRole.AFFECTED_CALLER,)),
        (middleware, route),
        reasoning_graph=_reasoning_graph(connected=True),
    )

    assert result.release_allowed is True
    assert {item.evidence_id for item in result.coalition} == {
        "caller-middleware",
        "caller-route",
    }
    assert not any(
        row.reason is rr.SuppressionReason.REDUNDANT_ROLE
        for row in result.suppressed
    )


def test_visibility_and_oracle_identity_are_scoped_to_the_exact_decision() -> None:
    target = _evidence(
        "target",
        role=rr.EvidenceRole.TARGET_IDENTITY,
        graph_node="symbol:refreshSession",
        subject="refreshSession",
        consequence="edit refreshSession",
        visible_to_decision_ids=("decision-old",),
    )

    new_decision = rr.select_evidence_coalition(
        _decision(
            decision_id="decision-new",
            required_roles=(rr.EvidenceRole.TARGET_IDENTITY,),
        ),
        (target,),
        reasoning_graph=_reasoning_graph(connected=True),
    )
    assert new_decision.decision_id == "decision-new"
    assert tuple(item.evidence_id for item in new_decision.coalition) == ("target",)

    old_decision = rr.select_evidence_coalition(
        _decision(
            decision_id="decision-old",
            required_roles=(rr.EvidenceRole.TARGET_IDENTITY,),
        ),
        (target,),
        reasoning_graph=_reasoning_graph(connected=True),
    )
    assert old_decision.decision_id == "decision-old"
    assert old_decision.coalition == ()
    suppression = next(
        row for row in old_decision.suppressed
        if row.evidence_id == "target"
    )
    assert suppression.reason is rr.SuppressionReason.ALREADY_VISIBLE
