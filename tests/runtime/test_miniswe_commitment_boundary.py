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


def test_mixed_batch_preserves_the_complete_native_batch() -> None:
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
        ({"id": "search"}, {"id": "read"}, {"id": "edit"}),
    ]
    assert result == [
        {"output": "search"},
        {"output": "read"},
        {"output": "edit"},
    ]
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


class _ToolCallModel:
    """The mini-swe 2.4.5 formatter contract, including its not-executed padding."""

    def format_observation_messages(self, message, outputs, template_vars=None):
        actions = message.get("extra", {}).get("actions", [])
        not_executed = {
            "output": "",
            "returncode": -1,
            "exception_info": "action was not executed",
        }
        padded = list(outputs) + [not_executed] * (len(actions) - len(outputs))
        rendered = []
        for action, output in zip(actions, padded):
            item = {"content": str(output.get("output", "")), "extra": dict(output)}
            if "tool_call_id" in action:
                item["tool_call_id"] = action["tool_call_id"]
                item["role"] = "tool"
            else:
                item["role"] = "user"
            rendered.append(item)
        return rendered


class ToolCallAgent:
    """A mini-swe DefaultAgent whose execute_actions APPENDS observation messages."""

    def __init__(self) -> None:
        self.model = _ToolCallModel()
        self.messages: list[dict] = []
        self.executed: list[tuple[dict, ...]] = []

    def get_template_vars(self) -> dict:
        return {}

    def add_messages(self, *messages: dict) -> list[dict]:
        self.messages.extend(messages)
        return list(messages)

    def execute_actions(self, message):
        actions = tuple(message["extra"]["actions"])
        self.executed.append(actions)
        outputs = [{"output": "ran:" + a["tool_call_id"], "returncode": 0} for a in actions]
        return self.add_messages(
            *self.model.format_observation_messages(
                message, outputs, self.get_template_vars()
            )
        )


def _tool_call_message(agent, actions):
    """The assistant turn as mini-swe records it, plus its committed tool calls."""
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": a["tool_call_id"]} for a in actions],
        "extra": {"response": {"id": "response-1"}, "actions": list(actions)},
    }
    agent.add_messages(message)
    return message


def _unanswered_tool_calls(agent) -> list[str]:
    """Every tool_call_id the conversation proposed and never answered.

    A non-empty result is the exact provider-400 state that ended smoke
    30434343516 at 5/5 tasks.
    """
    answered = {
        message["tool_call_id"]
        for message in agent.messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    proposed = [
        call["id"]
        for message in agent.messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or ()
    ]
    return [call_id for call_id in proposed if call_id not in answered]


def _qualifying(action_ids):
    return CommitmentEvidence(
        evidence_id="GT-E1",
        decision_id="PATCH_CONSTRUCTION",
        grade=EvidenceGrade.VERIFIED,
        lifecycle=EvidenceLifecycle.RELEASED,
        fresh=True,
        superseded=False,
        release_allowed=True,
        visible_to_model_call_ids=(),
        material_action_ids=tuple(action_ids),
    )


def test_fresh_inference_answers_every_deferred_tool_call() -> None:
    agent = ToolCallAgent()
    intent = _intent("edit", ActionOperation.EDIT)
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(
            (intent,),
            evidence=(_qualifying(("edit",)),),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
        ),
    )
    actions = [{"id": "edit", "tool_call_id": "call-1"}]
    message = _tool_call_message(agent, actions)

    result = agent.execute_actions(message)

    # The gate still holds: nothing ran.
    assert agent.executed == []
    # ...but the host conversation is still valid.
    assert _unanswered_tool_calls(agent) == []
    assert [item["role"] for item in result] == ["tool"]
    assert result[0]["tool_call_id"] == "call-1"
    assert result[0]["extra"]["exception_info"] == "action was not executed"


def test_mixed_tool_call_batch_executes_without_synthetic_hold() -> None:
    agent = ToolCallAgent()
    intents = (
        _intent("search", ActionOperation.SEARCH),
        _intent("edit", ActionOperation.EDIT),
    )
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(intents),
    )
    actions = [
        {"id": "search", "tool_call_id": "call-1"},
        {"id": "edit", "tool_call_id": "call-2"},
    ]
    message = _tool_call_message(agent, actions)

    agent.execute_actions(message)

    assert agent.executed == [
        (
            {"id": "search", "tool_call_id": "call-1"},
            {"id": "edit", "tool_call_id": "call-2"},
        )
    ]
    assert _unanswered_tool_calls(agent) == []
    answers = [item for item in agent.messages if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in answers] == ["call-1", "call-2"]
    assert answers[1]["content"] == "ran:call-2"


def test_unanswerable_host_executes_natively_instead_of_stranding_tool_calls(
    monkeypatch,
) -> None:
    agent = ToolCallAgent()
    intent = _intent("edit", ActionOperation.EDIT)
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(
            (intent,),
            evidence=(_qualifying(("edit",)),),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
        ),
    )
    actions = [{"id": "edit", "tool_call_id": "call-1"}]
    message = _tool_call_message(agent, actions)
    monkeypatch.setattr(
        MiniSweCommitmentBoundary,
        "_unexecuted_observations",
        classmethod(lambda *_args, **_kwargs: None),
    )

    agent.execute_actions(message)

    assert agent.executed == [({"id": "edit", "tool_call_id": "call-1"},)]
    assert _unanswered_tool_calls(agent) == []


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


def test_withheld_tool_answer_names_the_hold_reason(monkeypatch) -> None:
    """H2 / #55-1 (ARCH-E + ARCH-F, 2026-07-29). Run 30478454517 withheld 2,421
    actions answered only by the bare 'action was not executed' padding; agents
    universally misread it as a broken shell and thrashed to the step cap (28/37
    trajectories). Every surveyed industry gate blocks WITH a reason — silent
    withholding has no shipped precedent. The withheld tool answer must say the
    action was HELD by GroundTruth (not a failure), name the plan's reason code,
    and say how to proceed. The briefcase task is the in-corpus positive control:
    explained withhold -> instant correct action -> resolve."""
    monkeypatch.delenv("GT_WITHHOLD_REASON", raising=False)
    agent = ToolCallAgent()
    intent = _intent("edit", ActionOperation.EDIT)
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(
            (intent,),
            evidence=(_qualifying(("edit",)),),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
        ),
    )
    actions = [{"id": "edit", "tool_call_id": "call-1"}]
    message = _tool_call_message(agent, actions)

    result = agent.execute_actions(message)

    assert agent.executed == []                      # the gate still holds
    assert _unanswered_tool_calls(agent) == []       # conversation still valid
    text = str(result[0].get("content") or "")
    assert "GroundTruth" in text and "held" in text.lower(), (
        "the withheld answer must attribute the hold, or the agent misreads "
        "it as a broken shell"
    )
    assert "VERIFIED_UNSEEN_MATERIAL_EVIDENCE" in text
    assert "re-issue" in text.lower()
    # the host's own padding marker stays — this is an APPEND, never a rewrite
    assert result[0]["extra"]["exception_info"] == "action was not executed"


def test_withhold_reason_kill_switch_restores_bare_padding(monkeypatch) -> None:
    monkeypatch.setenv("GT_WITHHOLD_REASON", "0")
    agent = ToolCallAgent()
    intent = _intent("edit", ActionOperation.EDIT)
    MiniSweCommitmentBoundary(
        agent=agent,
        context_builder=lambda _message: _context(
            (intent,),
            evidence=(_qualifying(("edit",)),),
            phase=BatchPhase.AFTER_EPISTEMIC_PREFIX,
        ),
    )
    actions = [{"id": "edit", "tool_call_id": "call-1"}]
    message = _tool_call_message(agent, actions)
    result = agent.execute_actions(message)
    assert str(result[0].get("content") or "") == ""
