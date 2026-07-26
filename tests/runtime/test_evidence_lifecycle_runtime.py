"""RED contract for revision-bound canonical evidence lifecycle."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REV = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)


def _evidence(*, lifecycle=rr.EvidenceLifecycle.PENDING) -> object:
    return rr.EvidenceRecord(
        evidence_id="GT-E144",
        feature_id="caller_contract",
        decision_context=rr.DecisionContext.PATCH_CONSTRUCTION,
        roles=(
            rr.EvidenceRole.BEHAVIORAL_CONTRACT,
            rr.EvidenceRole.AFFECTED_CALLER,
        ),
        subject="src/auth/session.ts::refreshSession",
        claim="Production callers require a Session return value.",
        actionable_consequence="Preserve caller-visible return semantics.",
        provenance=("src/auth/middleware.ts::refreshRequest",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REV,
        causal_neighborhood=("decision:refresh-session", "symbol:refreshSession"),
        lifecycle=lifecycle,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=40,
        failure_prevention=5,
        causal_value=4,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=("nodes", "edges", "props_rev"),
        transition_history=(),
    )


def _request(state, reason: str) -> object:
    return rr.EvidenceTransitionRequest(to_state=state, reason_code=reason)


def _delivered_attempt(evidence_id: str) -> object:
    attempt = rr.DeliveryAttempt(
        evidence_ids=(evidence_id,),
        capsule_hash="a" * 64,
        model_call_id="call-1",
    )
    attempt = rr.advance_delivery(
        attempt,
        rr.DeliveryState.COMPILED,
        observation_id="obs-1",
    )
    attempt = rr.advance_delivery(
        attempt,
        rr.DeliveryState.JOINED,
        joined_capsule_hash="a" * 64,
        provider_payload_hash="b" * 64,
    )
    attempt = rr.advance_delivery(attempt, rr.DeliveryState.DISPATCHED)
    attempt = rr.advance_delivery(
        attempt,
        rr.DeliveryState.PROVIDER_ACCEPTED,
        provider_response_id="resp-1",
    )
    return rr.record_provider_terminal(
        attempt,
        rr.ModelCallAttempt(
            model_call_id="call-1",
            joined_capsule_hash="a" * 64,
            provider_payload_hash="b" * 64,
            provider_response_id="resp-1",
            terminal_kind=rr.ProviderTerminalKind.COMPLETED,
        ),
    )


def test_evidence_follows_reason_coded_pending_ready_held_released_lifecycle() -> None:
    requests = (
        _request(rr.EvidenceLifecycle.READY, "READINESS_RULES_SATISFIED"),
        _request(rr.EvidenceLifecycle.HELD, "OTHER_DECISION_CURRENTLY_ACTIVE"),
        _request(rr.EvidenceLifecycle.RELEASED, "DECISION_WINDOW_OPEN"),
    )
    result = rr.replay_evidence_transitions(_evidence(), requests)
    assert result.lifecycle is rr.EvidenceLifecycle.RELEASED
    assert [
        (row.from_state, row.to_state, row.reason_code)
        for row in result.transition_history
    ] == [
        (
            rr.EvidenceLifecycle.PENDING,
            rr.EvidenceLifecycle.READY,
            "READINESS_RULES_SATISFIED",
        ),
        (
            rr.EvidenceLifecycle.READY,
            rr.EvidenceLifecycle.HELD,
            "OTHER_DECISION_CURRENTLY_ACTIVE",
        ),
        (
            rr.EvidenceLifecycle.HELD,
            rr.EvidenceLifecycle.RELEASED,
            "DECISION_WINDOW_OPEN",
        ),
    ]


def test_every_lifecycle_transition_requires_a_nonempty_reason_code() -> None:
    with pytest.raises(rr.EvidenceLifecycleError, match="reason"):
        rr.transition_evidence(
            _evidence(),
            rr.EvidenceLifecycle.READY,
            reason_code="",
        )


def test_released_cannot_become_delivered_without_provider_terminal_attempt() -> None:
    released = rr.transition_evidence(
        rr.transition_evidence(
            _evidence(),
            rr.EvidenceLifecycle.READY,
            reason_code="READINESS_RULES_SATISFIED",
        ),
        rr.EvidenceLifecycle.RELEASED,
        reason_code="DECISION_WINDOW_OPEN",
    )
    with pytest.raises(rr.EvidenceLifecycleError, match="provider"):
        rr.transition_evidence(
            released,
            rr.EvidenceLifecycle.DELIVERED,
            reason_code="MODEL_EXPOSURE_PROVEN",
        )


def test_provider_terminal_attempt_must_bind_the_same_evidence() -> None:
    released = rr.replay_evidence_transitions(
        _evidence(),
        (
            _request(rr.EvidenceLifecycle.READY, "READY"),
            _request(rr.EvidenceLifecycle.RELEASED, "RELEASE"),
        ),
    )
    wrong = _delivered_attempt("GT-E999")
    with pytest.raises(rr.EvidenceLifecycleError, match="evidence"):
        rr.transition_evidence(
            released,
            rr.EvidenceLifecycle.DELIVERED,
            reason_code="MODEL_EXPOSURE_PROVEN",
            delivery_attempt=wrong,
        )

    correct = _delivered_attempt("GT-E144")
    delivered = rr.transition_evidence(
        released,
        rr.EvidenceLifecycle.DELIVERED,
        reason_code="MODEL_EXPOSURE_PROVEN",
        delivery_attempt=correct,
    )
    assert delivered.lifecycle is rr.EvidenceLifecycle.DELIVERED
    assert delivered.transition_history[-1].reason_code is (
        rr.EvidenceTransitionReason.PROVIDER_TERMINAL_DELIVERY_PROVEN
    )


def test_declared_revision_dependency_change_invalidates_evidence() -> None:
    ready = rr.transition_evidence(
        _evidence(),
        rr.EvidenceLifecycle.READY,
        reason_code="READY",
    )
    lsp_only = rr.RevisionVector(
        repository_content="repo-1",
        graph="graph-1",
        lsp="lsp-2",
        runtime_evidence="runtime-1",
    )
    assert rr.invalidate_stale_evidence(ready, current_revision=lsp_only) is ready

    graph_changed = rr.RevisionVector(
        repository_content="repo-1",
        graph="graph-2",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )
    invalidated = rr.invalidate_stale_evidence(
        ready,
        current_revision=graph_changed,
    )
    assert invalidated.lifecycle is rr.EvidenceLifecycle.INVALIDATED
    assert invalidated.fresh is False
    assert invalidated.transition_history[-1].reason_code is (
        rr.EvidenceTransitionReason.REVISION_DEPENDENCY_CHANGED
    )
    assert invalidated.transition_history[-1].reason_detail == (
        "nodes,edges,props_rev"
    )


def test_evidence_replay_is_byte_deterministic_and_immutable() -> None:
    requests = (
        _request(rr.EvidenceLifecycle.READY, "READY"),
        _request(rr.EvidenceLifecycle.HELD, "HELD"),
        _request(rr.EvidenceLifecycle.RELEASED, "RELEASED"),
    )
    a = rr.replay_evidence_transitions(_evidence(), requests)
    b = rr.replay_evidence_transitions(_evidence(), requests)
    assert a == b
    assert a.canonical_json() == b.canonical_json()
    assert a.content_hash == b.content_hash
    with pytest.raises(FrozenInstanceError):
        a.lifecycle = rr.EvidenceLifecycle.EXPIRED  # type: ignore[misc]
