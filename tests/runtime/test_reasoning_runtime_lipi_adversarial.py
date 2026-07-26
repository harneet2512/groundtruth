"""Wave-4 RED contracts for cross-layer reasoning-runtime integrity.

These tests exercise failures that superficially valid value objects can hide:

* release is not model exposure and cannot activate/satisfy evidence;
* lifecycle reasons are typed transition predicates, not audit prose;
* a "verified contradiction" is mandatory only when both its grade and
  semantic authority justify that claim;
* a registered feature cannot emit evidence for a decision or reasoning role
  outside its canonical feature contract.
"""
from __future__ import annotations

from enum import Enum

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)


def _evidence_kwargs(**overrides):
    values = {
        "evidence_id": "GT-E144",
        "feature_id": "caller_contract",
        "decision_context": rr.DecisionContext.PATCH_CONSTRUCTION,
        "roles": (
            rr.EvidenceRole.BEHAVIORAL_CONTRACT,
            rr.EvidenceRole.AFFECTED_CALLER,
        ),
        "subject": "src/auth/session.ts::refreshSession",
        "claim": "Production callers require a Session return value.",
        "actionable_consequence": "Preserve caller-visible return semantics.",
        "provenance": ("src/auth/middleware.ts::refreshRequest",),
        "grade": rr.EvidenceGrade.VERIFIED,
        "revision": REVISION,
        "causal_neighborhood": (
            "decision:refresh-session",
            "symbol:refreshSession",
        ),
        "lifecycle": rr.EvidenceLifecycle.READY,
        "fresh": True,
        "already_visible": False,
        "superseded": False,
        "mandatory_reason": None,
        "token_cost": 40,
        "failure_prevention": 5,
        "causal_value": 4,
        "contradiction_resolution": 0,
        "anchoring_risk": 0,
        "revision_dependencies": ("nodes", "edges", "props_rev"),
        "transition_history": (),
    }
    values.update(overrides)
    return values


def _activation_reason():
    reason_type = getattr(rr, "EvidenceTransitionReason", None)
    if reason_type is None:
        return "ACTIVATED_AFTER_PROVIDER_DELIVERY"
    return reason_type.ACTIVATED_AFTER_PROVIDER_DELIVERY


@pytest.mark.parametrize(
    "target",
    (
        rr.EvidenceLifecycle.ACTIVE,
        rr.EvidenceLifecycle.SATISFIED,
    ),
)
def test_released_evidence_cannot_activate_or_satisfy_without_delivery_proof(
    target,
) -> None:
    released = rr.EvidenceRecord(
        **_evidence_kwargs(lifecycle=rr.EvidenceLifecycle.RELEASED)
    )

    with pytest.raises(
        rr.EvidenceLifecycleError,
        match="provider|DELIVERED|delivery",
    ):
        rr.transition_evidence(
            released,
            target,
            reason_code=_activation_reason(),
        )


def test_lifecycle_reason_type_exists_and_is_an_enum() -> None:
    reason_type = getattr(rr, "EvidenceTransitionReason", None)

    assert reason_type is not None, (
        "lifecycle transition reasons need a canonical executable enum"
    )
    assert issubclass(reason_type, Enum)
    assert {
        "READINESS_RULES_SATISFIED",
        "DECISION_WINDOW_OPEN",
        "PROVIDER_TERMINAL_DELIVERY_PROVEN",
        "ACTIVATED_AFTER_PROVIDER_DELIVERY",
        "DECISION_SATISFIED",
    } <= set(reason_type.__members__)


def test_arbitrary_lifecycle_reason_strings_are_rejected() -> None:
    pending = rr.EvidenceRecord(
        **_evidence_kwargs(lifecycle=rr.EvidenceLifecycle.PENDING)
    )

    with pytest.raises(
        (TypeError, rr.EvidenceLifecycleError),
        match="typed|reason|EvidenceTransitionReason",
    ):
        rr.transition_evidence(
            pending,
            rr.EvidenceLifecycle.READY,
            reason_code="WHATEVER_THE_CALLER_WANTS",
        )


def test_typed_reason_is_executable_only_for_its_declared_transition() -> None:
    reason_type = getattr(rr, "EvidenceTransitionReason", None)
    assert reason_type is not None

    pending = rr.EvidenceRecord(
        **_evidence_kwargs(lifecycle=rr.EvidenceLifecycle.PENDING)
    )
    ready = rr.transition_evidence(
        pending,
        rr.EvidenceLifecycle.READY,
        reason_code=reason_type.READINESS_RULES_SATISFIED,
    )
    assert isinstance(
        ready.transition_history[-1].reason_code,
        reason_type,
    )

    with pytest.raises(rr.EvidenceLifecycleError, match="reason|transition"):
        rr.transition_evidence(
            ready,
            rr.EvidenceLifecycle.RELEASED,
            reason_code=reason_type.READINESS_RULES_SATISFIED,
        )


def test_verified_contradiction_requires_verified_grade() -> None:
    with pytest.raises(
        ValueError,
        match="verified contradiction|VERIFIED|grade",
    ):
        rr.EvidenceRecord(
            **_evidence_kwargs(
                grade=rr.EvidenceGrade.WARNING,
                roles=(rr.EvidenceRole.CONTRADICTION,),
                mandatory_reason=rr.MandatoryReason.VERIFIED_CONTRADICTION,
            )
        )


def test_verified_contradiction_rejects_weak_semantic_authority() -> None:
    with pytest.raises(
        (ValueError, rr.EvidenceLifecycleError),
        match="verified contradiction|authority|VERIFIED",
    ):
        rr.EvidenceRecord(
            **_evidence_kwargs(
                roles=(rr.EvidenceRole.CONTRADICTION,),
                mandatory_reason=rr.MandatoryReason.VERIFIED_CONTRADICTION,
                authority=rr.Authority.COMMAND_FALLBACK,
            )
        )


def test_verified_contradiction_accepts_verified_grade_and_authority() -> None:
    evidence = rr.EvidenceRecord(
        **_evidence_kwargs(
            roles=(rr.EvidenceRole.CONTRADICTION,),
            mandatory_reason=rr.MandatoryReason.VERIFIED_CONTRADICTION,
            authority=rr.Authority.RESULT_DERIVED,
        )
    )

    assert evidence.grade is rr.EvidenceGrade.VERIFIED
    assert evidence.authority is rr.Authority.RESULT_DERIVED


def test_registered_feature_rejects_incompatible_decision_context() -> None:
    with pytest.raises(
        ValueError,
        match="feature contract|decision.context|decision_context",
    ):
        rr.EvidenceRecord(
            **_evidence_kwargs(
                decision_context=rr.DecisionContext.COMPLETION,
            )
        )


def test_registered_feature_rejects_roles_outside_its_contract() -> None:
    with pytest.raises(
        ValueError,
        match="feature contract|role",
    ):
        rr.EvidenceRecord(
            **_evidence_kwargs(
                roles=(rr.EvidenceRole.VALIDATION,),
            )
        )
