from __future__ import annotations

from dataclasses import replace

from groundtruth.runtime.commitment_control import (
    BatchPhase,
    CommitmentControlContext,
    CommitmentEvidence,
    CommitmentIntent,
)
from groundtruth.runtime.miniswe_commitment_boundary import (
    MiniSweCommitmentBoundary,
)
from groundtruth.runtime.reasoning_runtime import (
    ActionOperation,
    CanonicalAction,
    EvidenceGrade,
    EvidenceLifecycle,
    FailurePolicyState,
)


def _intent(action_id: str, operation: ActionOperation) -> CommitmentIntent:
    return CommitmentIntent(
        CanonicalAction(
            action_id=action_id,
            operation=operation,
            tool_family="structured",
            tool_name="mini",
            structured_operation=operation.value,
            subject=action_id,
        ),
        sandboxed=operation is ActionOperation.EDIT,
    )


class Agent:
    def __init__(self) -> None:
        self.executed: list[tuple[dict, ...]] = []

    def execute_actions(self, message):
        actions = tuple(message["extra"]["actions"])
        self.executed.append(actions)
        return [{"output": action["id"]} for action in actions]


def _context(intents, *, evidence=(), phase=BatchPhase.BEFORE_BATCH):
    return CommitmentControlContext(
        intents=tuple(intents),
        phase=phase,
        active_decision_id="PATCH_CONSTRUCTION",
        proposing_model_call_id="model-1",
        evidence=tuple(evidence),
        failure_state=FailurePolicyState.initial(attempt_id="attempt-1"),
        epistemic_prefix_may_change_decision=True,
        certificate_requirements_met=True,
    )


def test_pause_executes_only_original_epistemic_prefix() -> None:
    agent = Agent()
    intents = (
        _intent("search", ActionOperation.SEARCH),
        _intent("read", ActionOperation.VIEW_SOURCE),
        _intent("edit", ActionOperation.EDIT),
    )
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(intents),
    )
    message = {
        "role": "assistant",
        "extra": {
            "response": {"id": "response-1"},
            "actions": [
                {"id": "search"},
                {"id": "read"},
                {"id": "edit"},
            ],
        },
    }

    result = agent.execute_actions(message)

    assert agent.executed == [
        ({"id": "search"}, {"id": "read"}),
    ]
    assert result == [{"output": "search"}, {"output": "read"}]
    assert [item["id"] for item in message["extra"]["actions"]] == [
        "search",
        "read",
        "edit",
    ]


def test_fresh_inference_executes_no_commitment_and_adds_no_direct_bytes() -> None:
    agent = Agent()
    intent = _intent("edit", ActionOperation.EDIT)
    evidence = CommitmentEvidence(
        evidence_id="GT-E1",
        decision_id="PATCH_CONSTRUCTION",
        grade=EvidenceGrade.VERIFIED,
        lifecycle=EvidenceLifecycle.RELEASED,
        fresh=True,
        superseded=False,
        release_allowed=True,
        visible_to_model_call_ids=(),
        material_action_ids=("edit",),
    )
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(
            (intent,),
            evidence=(evidence,),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
        ),
    )

    assert agent.execute_actions(
        {"extra": {"actions": [{"id": "edit"}]}}
    ) == []
    assert agent.executed == []


def test_allow_preserves_exact_native_message_object() -> None:
    agent = Agent()
    intent = _intent("test", ActionOperation.TEST)
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: replace(
            _context((intent,)),
            epistemic_prefix_may_change_decision=False,
        ),
    )
    action = {"id": "test"}
    message = {"extra": {"actions": [action]}}

    agent.execute_actions(message)

    assert agent.executed == [(action,)]
