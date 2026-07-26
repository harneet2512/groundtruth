from __future__ import annotations

from dataclasses import replace

from groundtruth.runtime.commitment_control import (
    ActionAssuranceClass,
    BatchPhase,
    CommitmentControlContext,
    CommitmentDecision,
    CommitmentEvidence,
    CommitmentIntent,
    classify_action,
    decide_commitment_control,
)
from groundtruth.runtime.reasoning_runtime import (
    ActionOperation,
    AssuranceStatus,
    CanonicalAction,
    EvidenceGrade,
    EvidenceLifecycle,
    FailurePolicyState,
    RuntimeHealthState,
)


def _action(
    action_id: str,
    operation: ActionOperation,
) -> CanonicalAction:
    return CanonicalAction(
        action_id=action_id,
        operation=operation,
        tool_family="structured",
        tool_name="native",
        structured_operation=operation.value.lower(),
        subject=f"subject:{action_id}",
    )


def _intent(
    action_id: str,
    operation: ActionOperation,
    **kwargs: object,
) -> CommitmentIntent:
    return CommitmentIntent(
        action=_action(action_id, operation),
        **kwargs,
    )


def _healthy() -> FailurePolicyState:
    return FailurePolicyState.initial(attempt_id="attempt-1")


def _context(
    *,
    intents: tuple[CommitmentIntent, ...],
    phase: BatchPhase = BatchPhase.BEFORE_BATCH,
    evidence: tuple[CommitmentEvidence, ...] = (),
    failure_state: FailurePolicyState | None = None,
    prefix_may_change_decision: bool = False,
    certificate_requirements_met: bool = True,
    model_call_id: str = "model-1",
) -> CommitmentControlContext:
    return CommitmentControlContext(
        intents=intents,
        phase=phase,
        active_decision_id="decision-1",
        proposing_model_call_id=model_call_id,
        evidence=evidence,
        failure_state=failure_state or _healthy(),
        epistemic_prefix_may_change_decision=prefix_may_change_decision,
        certificate_requirements_met=certificate_requirements_met,
    )


def _material_evidence(
    *,
    visible_to: tuple[str, ...] = (),
    fresh: bool = True,
    grade: EvidenceGrade = EvidenceGrade.VERIFIED,
    release_allowed: bool = True,
    lifecycle: EvidenceLifecycle = EvidenceLifecycle.RELEASED,
    material_action_ids: tuple[str, ...] = ("edit-1",),
) -> CommitmentEvidence:
    return CommitmentEvidence(
        evidence_id="GT-E1",
        decision_id="decision-1",
        grade=grade,
        lifecycle=lifecycle,
        fresh=fresh,
        superseded=False,
        release_allowed=release_allowed,
        visible_to_model_call_ids=visible_to,
        material_action_ids=material_action_ids,
    )


def test_classification_uses_structured_assurance_facts() -> None:
    assert (
        classify_action(_intent("read", ActionOperation.VIEW_SOURCE))
        is ActionAssuranceClass.EPISTEMIC
    )
    assert (
        classify_action(
            _intent("edit", ActionOperation.EDIT, sandboxed=True)
        )
        is ActionAssuranceClass.REVERSIBLE_COMMITMENT
    )
    assert (
        classify_action(
            _intent("edit", ActionOperation.EDIT, sandboxed=False)
        )
        is ActionAssuranceClass.HIGH_IMPACT_COMMITMENT
    )
    assert (
        classify_action(_intent("sig", ActionOperation.SIGNATURE_CHANGE))
        is ActionAssuranceClass.HIGH_IMPACT_COMMITMENT
    )
    assert (
        classify_action(_intent("submit", ActionOperation.SUBMIT))
        is ActionAssuranceClass.TERMINAL_COMMITMENT
    )


def test_mixed_batch_executes_only_epistemic_prefix_when_it_may_change_decision() -> None:
    search = _intent("search-1", ActionOperation.SEARCH)
    read = _intent("read-1", ActionOperation.VIEW_SOURCE)
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)

    plan = decide_commitment_control(
        _context(
            intents=(search, read, edit),
            prefix_may_change_decision=True,
        )
    )

    assert plan.decision is CommitmentDecision.PAUSE
    assert plan.execute_now == (search, read)
    assert plan.deferred == (edit,)
    assert plan.epistemic_prefix == (search, read)
    assert plan.reason_code == "EPISTEMIC_PREFIX_FIRST"
    assert plan.native_path_preserved is True


def test_mixed_batch_is_not_paused_without_a_material_evidence_opportunity() -> None:
    search = _intent("search-1", ActionOperation.SEARCH)
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)

    plan = decide_commitment_control(
        _context(intents=(search, edit), prefix_may_change_decision=False)
    )

    assert plan.decision is CommitmentDecision.ALLOW
    assert plan.execute_now == (search, edit)
    assert plan.deferred == ()


def test_post_prefix_verified_fresh_unseen_material_evidence_requires_fresh_inference() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)

    plan = decide_commitment_control(
        _context(
            intents=(edit,),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
            evidence=(_material_evidence(),),
        )
    )

    assert plan.decision is CommitmentDecision.FRESH_INFERENCE
    assert plan.execute_now == ()
    assert plan.deferred == (edit,)
    assert plan.qualifying_evidence_ids == ("GT-E1",)
    assert plan.fresh_inference_required is True


def test_preexisting_material_evidence_interrupts_a_single_commitment() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)

    plan = decide_commitment_control(
        _context(intents=(edit,), evidence=(_material_evidence(),))
    )

    assert plan.decision is CommitmentDecision.FRESH_INFERENCE
    assert plan.execute_now == ()
    assert plan.deferred == (edit,)


def test_evidence_must_be_verified_fresh_unseen_released_and_material() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)
    candidates = (
        _material_evidence(grade=EvidenceGrade.WARNING),
        replace(_material_evidence(), evidence_id="GT-E2", fresh=False),
        replace(
            _material_evidence(),
            evidence_id="GT-E3",
            visible_to_model_call_ids=("model-1",),
        ),
        replace(
            _material_evidence(),
            evidence_id="GT-E4",
            release_allowed=False,
        ),
        replace(
            _material_evidence(),
            evidence_id="GT-E5",
            material_action_ids=("other-edit",),
        ),
        replace(
            _material_evidence(),
            evidence_id="GT-E6",
            decision_id="decision-elsewhere",
        ),
        replace(
            _material_evidence(),
            evidence_id="GT-E7",
            superseded=True,
        ),
        replace(
            _material_evidence(),
            evidence_id="GT-E8",
            lifecycle=EvidenceLifecycle.PENDING,
        ),
    )

    plan = decide_commitment_control(
        _context(
            intents=(edit,),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
            evidence=candidates,
        )
    )

    assert plan.decision is CommitmentDecision.ALLOW
    assert plan.execute_now == (edit,)
    assert plan.qualifying_evidence_ids == ()


def test_fresh_inference_is_not_repeated_after_exact_model_call_visibility() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)
    plan = decide_commitment_control(
        _context(
            intents=(edit,),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
            evidence=(_material_evidence(visible_to=("model-2",)),),
            model_call_id="model-2",
        )
    )

    assert plan.decision is CommitmentDecision.ALLOW
    assert plan.execute_now == (edit,)
    assert plan.fresh_inference_required is False


def test_quarantine_preserves_native_sandbox_path_and_marks_unassured() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)
    quarantined = replace(
        _healthy(),
        health=RuntimeHealthState.QUARANTINED,
        assurance=AssuranceStatus.UNASSURED,
        gt_emission_enabled=False,
        gt_interruption_enabled=False,
        gt_certification_enabled=False,
        native_path_enabled=True,
    )

    plan = decide_commitment_control(
        _context(
            intents=(edit,),
            failure_state=quarantined,
            evidence=(_material_evidence(),),
        )
    )

    assert plan.decision is CommitmentDecision.UNASSURED
    assert plan.execute_now == (edit,)
    assert plan.deferred == ()
    assert plan.native_path_preserved is True
    assert plan.gt_certificate_allowed is False
    assert plan.fresh_inference_required is False


def test_quarantined_terminal_action_blocks_only_gt_certificate() -> None:
    submit = _intent("submit-1", ActionOperation.SUBMIT)
    quarantined = replace(
        _healthy(),
        health=RuntimeHealthState.QUARANTINED,
        assurance=AssuranceStatus.UNASSURED,
        gt_emission_enabled=False,
        gt_interruption_enabled=False,
        gt_certification_enabled=False,
        native_path_enabled=True,
    )

    plan = decide_commitment_control(
        _context(intents=(submit,), failure_state=quarantined)
    )

    assert plan.decision is CommitmentDecision.BLOCK_CERTIFICATE
    assert plan.execute_now == (submit,)
    assert plan.native_path_preserved is True
    assert plan.gt_certificate_allowed is False
    assert plan.reason_code == "GT_UNAVAILABLE_TERMINAL_UNCERTIFIED"


def test_terminal_certificate_is_withheld_when_requirements_are_incomplete() -> None:
    submit = _intent("submit-1", ActionOperation.SUBMIT)

    plan = decide_commitment_control(
        _context(
            intents=(submit,),
            certificate_requirements_met=False,
        )
    )

    assert plan.decision is CommitmentDecision.BLOCK_CERTIFICATE
    assert plan.execute_now == (submit,)
    assert plan.gt_certificate_allowed is False
    assert plan.native_path_preserved is True


def test_high_impact_action_is_not_blanket_blocked_when_gt_is_healthy() -> None:
    signature_change = _intent(
        "sig-1",
        ActionOperation.SIGNATURE_CHANGE,
        public_contract_change=True,
    )

    plan = decide_commitment_control(_context(intents=(signature_change,)))

    assert plan.decision is CommitmentDecision.ALLOW
    assert plan.execute_now == (signature_change,)
    assert plan.native_path_preserved is True


def test_degraded_runtime_allows_reversible_work_but_not_clean_terminal_certificate() -> None:
    degraded = replace(
        _healthy(),
        health=RuntimeHealthState.DEGRADED,
        assurance=AssuranceStatus.DEGRADED,
        gt_certification_enabled=False,
    )
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)
    submit = _intent("submit-1", ActionOperation.SUBMIT)

    edit_plan = decide_commitment_control(
        _context(intents=(edit,), failure_state=degraded)
    )
    submit_plan = decide_commitment_control(
        _context(intents=(submit,), failure_state=degraded)
    )

    assert edit_plan.decision is CommitmentDecision.ALLOW
    assert edit_plan.execute_now == (edit,)
    assert edit_plan.gt_certificate_allowed is False
    assert submit_plan.decision is CommitmentDecision.BLOCK_CERTIFICATE
    assert submit_plan.execute_now == (submit,)


def test_non_prefix_epistemic_actions_are_not_reordered_across_commitments() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)
    test = _intent("test-1", ActionOperation.TEST)

    plan = decide_commitment_control(
        _context(
            intents=(edit, test),
            prefix_may_change_decision=True,
        )
    )

    assert plan.decision is CommitmentDecision.ALLOW
    assert plan.execute_now == (edit, test)
    assert plan.epistemic_prefix == ()


def test_duplicate_action_or_evidence_identity_is_rejected() -> None:
    edit = _intent("edit-1", ActionOperation.EDIT, sandboxed=True)
    try:
        _context(intents=(edit, edit))
    except ValueError as exc:
        assert "action identities" in str(exc)
    else:
        raise AssertionError("duplicate action identity was accepted")

    try:
        _context(
            intents=(edit,),
            evidence=(_material_evidence(), _material_evidence()),
        )
    except ValueError as exc:
        assert "evidence identities" in str(exc)
    else:
        raise AssertionError("duplicate evidence identity was accepted")


def test_empty_batch_is_rejected() -> None:
    try:
        _context(intents=())
    except ValueError as exc:
        assert "intents" in str(exc)
    else:
        raise AssertionError("empty commitment batch was accepted")
