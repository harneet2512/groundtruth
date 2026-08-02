from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import groundtruth.runtime.verification_plan as verification_plan_module
from groundtruth.runtime.commitment_control import (
    BatchPhase,
    CommitmentControlContext,
    CommitmentDecision,
    CommitmentEvidence,
    CommitmentIntent,
    decide_commitment_control,
)
from groundtruth.runtime.reasoning_runtime import (
    ActionOperation,
    CanonicalAction,
    EvidenceGrade,
    EvidenceLifecycle,
    FailurePolicyState,
)
from groundtruth.runtime.verification_plan import (
    Check,
    VerificationPlan,
    run_plan,
)


def _intent(action_id: str, subject: str) -> CommitmentIntent:
    return CommitmentIntent(
        action=CanonicalAction(
            action_id=action_id,
            operation=ActionOperation.EDIT,
            tool_family="structured",
            tool_name="mini",
            structured_operation="edit",
            subject=subject,
            targets=(subject,),
        ),
        sandboxed=True,
    )


def _evidence(action_id: str, *, staged: bool = True) -> CommitmentEvidence:
    return CommitmentEvidence(
        evidence_id="GT-E1",
        decision_id="PATCH_CONSTRUCTION",
        grade=EvidenceGrade.VERIFIED,
        lifecycle=EvidenceLifecycle.RELEASED,
        fresh=True,
        superseded=False,
        release_allowed=True,
        visible_to_model_call_ids=(),
        material_action_ids=(action_id,),
        staged_for_next_inference=staged,
    )


def _context(
    intent: CommitmentIntent,
    evidence: CommitmentEvidence,
    *,
    revision: str = "rev-a",
    consumed: tuple[str, ...] = (),
) -> CommitmentControlContext:
    return CommitmentControlContext(
        intents=(intent,),
        phase=BatchPhase.BEFORE_BATCH,
        active_decision_id="PATCH_CONSTRUCTION",
        proposing_model_call_id="model-1",
        evidence=(evidence,),
        failure_state=FailurePolicyState.initial(attempt_id="attempt-1"),
        epistemic_prefix_may_change_decision=False,
        certificate_requirements_met=True,
        repository_revision=revision,
        consumed_interruption_keys=consumed,
    )


def test_pre_edit_interruption_requires_successfully_staged_evidence() -> None:
    intent = _intent("edit-1", "src/a.py")

    plan = decide_commitment_control(
        _context(intent, _evidence("edit-1", staged=False))
    )

    assert plan.decision is CommitmentDecision.ALLOW
    assert plan.execute_now == (intent,)
    assert plan.interruption_key == ""


def test_equivalent_reissued_edit_is_interrupted_only_once() -> None:
    first = _intent("edit-call-1", "src/a.py")
    first_plan = decide_commitment_control(
        _context(first, _evidence("edit-call-1"))
    )
    assert first_plan.decision is CommitmentDecision.FRESH_INFERENCE
    assert first_plan.interruption_key

    reissued = _intent("edit-call-2", "src/a.py")
    second_plan = decide_commitment_control(
        _context(
            reissued,
            replace(
                _evidence("edit-call-1"),
                material_action_ids=("edit-call-2",),
            ),
            consumed=(first_plan.interruption_key,),
        )
    )

    assert second_plan.decision is CommitmentDecision.ALLOW
    assert second_plan.execute_now == (reissued,)
    assert second_plan.reason_code == "INTERRUPTION_ALREADY_CONSUMED"
    assert second_plan.interruption_key == first_plan.interruption_key


def test_target_or_repository_revision_change_rearms_interruption() -> None:
    first = _intent("edit-call-1", "src/a.py")
    first_plan = decide_commitment_control(
        _context(first, _evidence("edit-call-1"))
    )

    changed_target = _intent("edit-call-2", "src/b.py")
    target_plan = decide_commitment_control(
        _context(
            changed_target,
            replace(
                _evidence("edit-call-1"),
                material_action_ids=("edit-call-2",),
            ),
            consumed=(first_plan.interruption_key,),
        )
    )
    changed_revision = _intent("edit-call-3", "src/a.py")
    revision_plan = decide_commitment_control(
        _context(
            changed_revision,
            replace(
                _evidence("edit-call-1"),
                material_action_ids=("edit-call-3",),
            ),
            revision="rev-b",
            consumed=(first_plan.interruption_key,),
        )
    )

    assert target_plan.decision is CommitmentDecision.FRESH_INFERENCE
    assert revision_plan.decision is CommitmentDecision.FRESH_INFERENCE
    assert target_plan.interruption_key != first_plan.interruption_key
    assert revision_plan.interruption_key != first_plan.interruption_key


def test_verification_plan_total_budget_is_global_across_rungs(
    monkeypatch,
) -> None:
    clock = {"now": 10.0}
    monkeypatch.setattr(
        verification_plan_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"]),
        raising=False,
    )
    calls: list[tuple[list[str], int]] = []

    def executor(command, _cwd, timeout):
        calls.append((command, timeout))
        clock["now"] += 6.0
        return 0, "1 passed", ""

    checks = tuple(
        Check(
            kind="integration",
            command=("pytest", target),
            selection_basis="config:pytest_ini",
            covered_entities=("edited",),
            confidence="medium",
        )
        for target in ("tests/a.py", "tests/b.py")
    )
    plan = VerificationPlan(
        patch_revision="rev-a",
        graph_revision="graph-a",
        changed_entities=("edited",),
        obligations=(),
        checks=checks,
    )

    results = run_plan(
        plan,
        executor=executor,
        repo_root="/repo",
        total_budget_seconds=5,
    )

    assert len(calls) == 1
    assert calls[0][1] <= 5
    assert results[1].executed is False
    assert results[1].verdict == "unavailable"
    assert results[1].detail["reason"] == "total_budget_exhausted"
