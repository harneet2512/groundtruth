"""RED contract for decision-complete evidence coalition selection.

The canonical oracle emits at most one *capsule*, not at most one feature.  A
capsule may contain several evidence items only when every item contributes a
new actionable reasoning role to the same open decision and causal
neighborhood.  These tests deliberately exercise a pure, deterministic API;
rendering and provider delivery belong to later boundaries.
"""
from __future__ import annotations

from groundtruth.runtime.reasoning_runtime import (
    ActiveDecision,
    DecisionContext,
    EvidenceGrade,
    EvidenceLifecycle,
    EvidenceRecord,
    EvidenceRole,
    MandatoryReason,
    RevisionVector,
    SuppressionReason,
    select_evidence_coalition,
)


def _revision() -> RevisionVector:
    return RevisionVector(
        repository_content="repo-1",
        graph="graph-1",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )


def _decision(
    *,
    context: DecisionContext = DecisionContext.PATCH_CONSTRUCTION,
    required_roles: tuple[EvidenceRole, ...] = (
        EvidenceRole.TARGET_IDENTITY,
        EvidenceRole.BEHAVIORAL_CONTRACT,
    ),
    budget: int = 100,
    useful_roles: tuple[EvidenceRole, ...] = (),
) -> ActiveDecision:
    return ActiveDecision(
        decision_id="decision-refresh-session",
        context=context,
        primary_claim="repair refreshSession without changing its caller-visible contract",
        required_roles=required_roles,
        causal_neighborhood=("symbol:refreshSession", "obligation:rotation"),
        token_budget=budget,
        current_revision=_revision(),
        useful_roles=useful_roles,
    )


def _evidence(
    evidence_id: str,
    role: EvidenceRole,
    *,
    context: DecisionContext = DecisionContext.PATCH_CONSTRUCTION,
    causal_neighborhood: tuple[str, ...] = ("symbol:refreshSession",),
    grade: EvidenceGrade = EvidenceGrade.VERIFIED,
    mandatory: MandatoryReason | None = None,
    tokens: int = 20,
    failure_prevention: int = 1,
    causal_value: int = 1,
    contradiction_resolution: int = 0,
    anchoring_risk: int = 0,
    lifecycle: EvidenceLifecycle = EvidenceLifecycle.READY,
    fresh: bool = True,
    already_visible: bool = False,
    superseded: bool = False,
    revision: RevisionVector | None = None,
    claim: str | None = None,
    actionable_consequence: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        feature_id=f"producer-{evidence_id}",
        decision_context=context,
        roles=(role,),
        subject="src/auth/session.py::refreshSession",
        claim=claim or f"claim-{evidence_id}",
        actionable_consequence=(
            actionable_consequence or f"action-{evidence_id}"
        ),
        provenance=(f"src/auth/session.py::{evidence_id}",),
        grade=grade,
        revision=revision or _revision(),
        causal_neighborhood=causal_neighborhood,
        lifecycle=lifecycle,
        fresh=fresh,
        already_visible=already_visible,
        superseded=superseded,
        mandatory_reason=mandatory,
        token_cost=tokens,
        failure_prevention=failure_prevention,
        causal_value=causal_value,
        contradiction_resolution=contradiction_resolution,
        anchoring_risk=anchoring_risk,
        revision_dependencies=("repository_content", "graph"),
    )


def _ids(decision) -> tuple[str, ...]:
    return tuple(ref.evidence_id for ref in decision.coalition)


def _suppression(decision, evidence_id: str):
    return next(row for row in decision.suppressed if row.evidence_id == evidence_id)


def test_one_capsule_serves_exactly_one_open_decision_and_causal_neighborhood():
    active = _decision()
    target = _evidence("target", EvidenceRole.TARGET_IDENTITY)
    wrong_decision = _evidence(
        "completion",
        EvidenceRole.TERMINAL_ASSURANCE,
        context=DecisionContext.COMPLETION,
    )
    disconnected = _evidence(
        "cache",
        EvidenceRole.BEHAVIORAL_CONTRACT,
        causal_neighborhood=("symbol:unrelatedCache",),
    )
    contract = _evidence("contract", EvidenceRole.BEHAVIORAL_CONTRACT)

    result = select_evidence_coalition(
        active,
        [wrong_decision, disconnected, contract, target],
    )

    assert result.decision_context is DecisionContext.PATCH_CONSTRUCTION
    assert set(_ids(result)) == {"target", "contract"}
    assert _suppression(result, "completion").reason is SuppressionReason.OTHER_DECISION
    assert _suppression(result, "cache").reason is SuppressionReason.DISCONNECTED
    assert all(
        ref.decision_context is result.decision_context for ref in result.coalition
    )


def test_blockers_verified_contradictions_and_material_uncertainties_are_mandatory():
    active = _decision(
        required_roles=(
            EvidenceRole.TARGET_IDENTITY,
            EvidenceRole.BLOCKER,
            EvidenceRole.CONTRADICTION,
            EvidenceRole.MATERIAL_UNCERTAINTY,
        ),
        budget=100,
    )
    target = _evidence("target", EvidenceRole.TARGET_IDENTITY, tokens=10)
    blocker = _evidence(
        "red-test",
        EvidenceRole.BLOCKER,
        mandatory=MandatoryReason.BLOCKER,
        tokens=10,
    )
    contradiction = _evidence(
        "counterexample",
        EvidenceRole.CONTRADICTION,
        mandatory=MandatoryReason.VERIFIED_CONTRADICTION,
        contradiction_resolution=10,
        tokens=10,
    )
    uncertainty = _evidence(
        "dynamic-edge",
        EvidenceRole.MATERIAL_UNCERTAINTY,
        grade=EvidenceGrade.WARNING,
        mandatory=MandatoryReason.MATERIAL_UNCERTAINTY,
        tokens=10,
    )

    result = select_evidence_coalition(
        active,
        [target, uncertainty, contradiction, blocker],
    )

    assert set(result.mandatory_items) == {
        "red-test",
        "counterexample",
        "dynamic-edge",
    }
    assert set(result.mandatory_items).issubset(set(_ids(result)))
    # Mandatory evidence is admitted before marginal optimization, with a
    # deterministic order independent of producer arrival order.
    assert _ids(result)[:3] == (
        "red-test",
        "counterexample",
        "dynamic-edge",
    )
    assert result.unresolved_roles == ()


def test_every_selected_item_adds_a_unique_actionable_reasoning_role():
    active = _decision()
    target_primary = _evidence(
        "target-a",
        EvidenceRole.TARGET_IDENTITY,
        failure_prevention=5,
    )
    target_duplicate = _evidence(
        "target-b",
        EvidenceRole.TARGET_IDENTITY,
        failure_prevention=1,
    )
    contract = _evidence("contract", EvidenceRole.BEHAVIORAL_CONTRACT)

    result = select_evidence_coalition(
        active,
        [target_duplicate, contract, target_primary],
    )

    assert set(_ids(result)) == {"target-a", "contract"}
    duplicate = _suppression(result, "target-b")
    assert duplicate.reason is SuppressionReason.REDUNDANT_ROLE
    assert duplicate.held is True
    selected_roles = [role for ref in result.coalition for role in ref.roles]
    assert len(selected_roles) == len(set(selected_roles))
    assert all(ref.actionable_consequence for ref in result.coalition)


def test_marginal_value_per_token_is_deterministic_across_input_order():
    active = _decision(
        required_roles=(EvidenceRole.TARGET_IDENTITY,),
        budget=35,
        useful_roles=(
            EvidenceRole.VALIDATION,
            EvidenceRole.AFFECTED_CALLER,
        ),
    )
    target = _evidence("target", EvidenceRole.TARGET_IDENTITY, tokens=20)
    cheap_validation = _evidence(
        "validation-cheap",
        EvidenceRole.VALIDATION,
        tokens=10,
        failure_prevention=5,
        causal_value=2,
    )
    expensive_caller = _evidence(
        "caller-expensive",
        EvidenceRole.AFFECTED_CALLER,
        tokens=30,
        failure_prevention=8,
        causal_value=3,
    )

    first = select_evidence_coalition(
        active,
        [target, expensive_caller, cheap_validation],
    )
    second = select_evidence_coalition(
        active,
        [cheap_validation, target, expensive_caller],
    )

    assert _ids(first) == _ids(second) == ("target", "validation-cheap")
    assert first.total_tokens == 30
    assert _suppression(first, "caller-expensive").reason is SuppressionReason.BUDGET


def test_item_provenance_is_preserved_and_weakest_required_link_caps_confidence():
    active = _decision(useful_roles=(EvidenceRole.HISTORICAL_SUPPORT,))
    target = _evidence(
        "target",
        EvidenceRole.TARGET_IDENTITY,
        grade=EvidenceGrade.VERIFIED,
    )
    contract = _evidence(
        "contract",
        EvidenceRole.BEHAVIORAL_CONTRACT,
        grade=EvidenceGrade.WARNING,
    )
    optional_history = _evidence(
        "history",
        EvidenceRole.HISTORICAL_SUPPORT,
        grade=EvidenceGrade.INFO,
        failure_prevention=2,
    )

    result = select_evidence_coalition(
        active,
        [optional_history, contract, target],
    )

    by_id = {ref.evidence_id: ref for ref in result.coalition}
    assert by_id["target"].grade is EvidenceGrade.VERIFIED
    assert by_id["contract"].grade is EvidenceGrade.WARNING
    assert by_id["history"].grade is EvidenceGrade.INFO
    assert by_id["contract"].provenance == (
        "src/auth/session.py::contract",
    )
    # INFO history is optional.  The required target + contract chain contains
    # a WARNING link, so the capsule may not claim VERIFIED confidence.
    assert result.overall_grade is EvidenceGrade.WARNING


def test_lifecycle_freshness_visibility_and_supersession_gate_eligibility():
    active = _decision(required_roles=(EvidenceRole.TARGET_IDENTITY,))
    ready = _evidence("ready", EvidenceRole.TARGET_IDENTITY)
    pending = _evidence(
        "pending",
        EvidenceRole.BEHAVIORAL_CONTRACT,
        lifecycle=EvidenceLifecycle.PENDING,
    )
    stale = _evidence(
        "stale",
        EvidenceRole.AFFECTED_CALLER,
        fresh=False,
    )
    stale_revision = RevisionVector(
        repository_content="repo-1",
        graph="graph-old",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )
    revision_stale = _evidence(
        "revision-stale",
        EvidenceRole.AFFECTED_CALLER,
        revision=stale_revision,
    )
    visible = _evidence(
        "visible",
        EvidenceRole.VALIDATION,
        already_visible=True,
    )
    superseded = _evidence(
        "superseded",
        EvidenceRole.STATE_DEPENDENCY,
        superseded=True,
    )

    result = select_evidence_coalition(
        active,
        [pending, stale, revision_stale, visible, superseded, ready],
    )

    assert _ids(result) == ("ready",)
    assert _suppression(result, "pending").reason is SuppressionReason.NOT_READY
    assert _suppression(result, "stale").reason is SuppressionReason.STALE
    assert _suppression(result, "revision-stale").reason is SuppressionReason.STALE
    assert _suppression(result, "visible").reason is SuppressionReason.ALREADY_VISIBLE
    assert _suppression(result, "superseded").reason is SuppressionReason.SUPERSEDED
    assert all(
        _suppression(result, evidence_id).held
        for evidence_id in ("pending", "stale", "visible", "superseded")
    )


def test_decision_budget_leaves_unselected_positive_value_evidence_held():
    active = _decision(
        required_roles=(EvidenceRole.TARGET_IDENTITY,),
        budget=20,
        useful_roles=(EvidenceRole.VALIDATION,),
    )
    target = _evidence("target", EvidenceRole.TARGET_IDENTITY, tokens=20)
    validation = _evidence(
        "validation",
        EvidenceRole.VALIDATION,
        tokens=10,
        failure_prevention=10,
    )

    result = select_evidence_coalition(active, [validation, target])

    assert _ids(result) == ("target",)
    assert result.total_tokens == active.token_budget
    held = _suppression(result, "validation")
    assert held.reason is SuppressionReason.BUDGET
    assert held.held is True


def test_size_one_is_the_normal_special_case_when_one_item_is_decision_complete():
    active = _decision(
        context=DecisionContext.FAILURE_RECOVERY,
        required_roles=(EvidenceRole.BLOCKER,),
        budget=40,
    )
    decisive = _evidence(
        "compiler-error",
        EvidenceRole.BLOCKER,
        context=DecisionContext.FAILURE_RECOVERY,
        mandatory=MandatoryReason.BLOCKER,
        causal_neighborhood=("symbol:refreshSession",),
        tokens=14,
    )

    result = select_evidence_coalition(active, [decisive])

    assert len(result.coalition) == 1
    assert _ids(result) == ("compiler-error",)
    assert result.mandatory_items == ("compiler-error",)
    assert result.coverage == (EvidenceRole.BLOCKER,)
    assert result.unresolved_roles == ()
    assert result.suppressed == ()


def test_incomplete_decision_is_not_releasable_or_overclaimed():
    active = _decision(
        required_roles=(
            EvidenceRole.TARGET_IDENTITY,
            EvidenceRole.BEHAVIORAL_CONTRACT,
        ),
        budget=60,
    )
    target = _evidence("target", EvidenceRole.TARGET_IDENTITY)
    optional = _evidence("history", EvidenceRole.HISTORICAL_SUPPORT)

    result = select_evidence_coalition(active, [target, optional])

    assert result.unresolved_roles == (EvidenceRole.BEHAVIORAL_CONTRACT,)
    assert result.decision_complete is False
    assert result.release_allowed is False
    assert result.overall_grade is EvidenceGrade.INFO
    assert (
        _suppression(result, "history").reason
        is SuppressionReason.NOT_ACTIONABLE_FOR_DECISION
    )


def test_mandatory_counterevidence_is_never_dropped_as_redundant_or_by_budget():
    active = _decision(
        required_roles=(EvidenceRole.CONTRADICTION,),
        budget=10,
    )
    first = _evidence(
        "counter-a",
        EvidenceRole.CONTRADICTION,
        mandatory=MandatoryReason.VERIFIED_CONTRADICTION,
        tokens=8,
    )
    second = _evidence(
        "counter-b",
        EvidenceRole.CONTRADICTION,
        mandatory=MandatoryReason.MATERIAL_UNCERTAINTY,
        tokens=8,
    )

    result = select_evidence_coalition(active, [second, first])

    assert _ids(result) == ("counter-a", "counter-b")
    assert result.mandatory_items == ("counter-a", "counter-b")
    assert result.over_budget is True
    assert result.release_allowed is False
    assert all(
        row.reason not in {
            SuppressionReason.BUDGET,
            SuppressionReason.REDUNDANT_ROLE,
        }
        for row in result.suppressed
    )


def test_duplicate_claim_and_consequence_are_deduped_even_across_roles():
    active = _decision(
        required_roles=(EvidenceRole.TARGET_IDENTITY,),
        useful_roles=(EvidenceRole.EXECUTION_REACHABILITY,),
    )
    primary = _evidence(
        "primary",
        EvidenceRole.TARGET_IDENTITY,
        claim="refreshSession is the production target",
        actionable_consequence="edit refreshSession",
        failure_prevention=5,
    )
    duplicate = _evidence(
        "duplicate",
        EvidenceRole.EXECUTION_REACHABILITY,
        claim="refreshSession is the production target",
        actionable_consequence="edit refreshSession",
        failure_prevention=1,
    )

    result = select_evidence_coalition(active, [duplicate, primary])

    assert _ids(result) == ("primary",)
    assert (
        _suppression(result, "duplicate").reason
        is SuppressionReason.DUPLICATE_CLAIM
    )
