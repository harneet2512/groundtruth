"""Mini-SWE batch boundary for canonical commitment control.

The boundary never parses evidence, renders GT text, or executes a deferred
commitment.  It applies a pure :class:`CommitmentControlPlan` to the host
agent's existing ``execute_actions`` call:

* ALLOW/UNASSURED/BLOCK_CERTIFICATE: preserve the native batch unchanged;
* PAUSE: execute only the original contiguous epistemic prefix;
* FRESH_INFERENCE: execute nothing so the run loop samples again with the
  already-staged canonical capsule.

WITHHOLDING AN ACTION IS NOT THE SAME AS DELETING ITS OBSERVATION (2026-07-29).
Mini-SWE proposes actions as provider TOOL CALLS, and the chat protocol requires
every ``tool_call_id`` in an assistant message to be answered by a ``tool``
message before the next completion.  ``DefaultAgent.execute_actions`` is the only
thing that appends those answers, so a boundary that returns early leaves the
tool calls unanswered and the very next ``model.query`` is a hard provider 400
("An assistant message with 'tool_calls' must be followed by tool messages")
which kills the run.  Smoke 30434343516 lost 5/5 tasks to exactly that.

So a deferred tool-call-bound action is still ANSWERED: the host's own
observation formatter renders each output it was not given as
``action was not executed``.  The action does not run -- the gate holds -- and
the conversation stays valid.  Actions with no ``tool_call_id`` (text-action
hosts) are dropped exactly as before, byte-identically.
"""

from __future__ import annotations

import os
from copy import copy
from types import MethodType
from typing import Any, Callable

from .commitment_control import (
    CommitmentControlContext,
    CommitmentControlPlan,
    CommitmentDecision,
    decide_commitment_control,
)


class MiniSweCommitmentBoundary:
    """Install one action-batch controller on a concrete Mini-SWE agent."""

    def __init__(
        self,
        *,
        agent: Any,
        context_builder: Callable[[dict[str, Any]], CommitmentControlContext],
        plan_observer: Callable[
            [CommitmentControlContext, CommitmentControlPlan, tuple[Any, ...]],
            None,
        ] | None = None,
    ):
        execute_actions = getattr(agent, "execute_actions", None)
        if not callable(execute_actions):
            raise ValueError("agent.execute_actions is required")
        self.agent = agent
        self.context_builder = context_builder
        self.plan_observer = plan_observer
        self._original_execute_actions = execute_actions
        self._plans: list[CommitmentControlPlan] = []
        self._install()

    @property
    def plans(self) -> tuple[CommitmentControlPlan, ...]:
        return tuple(self._plans)

    @staticmethod
    def _message_with_actions(
        message: dict[str, Any],
        actions: tuple[Any, ...],
    ) -> dict[str, Any]:
        subset = copy(message)
        extra = copy(message.get("extra") or {})
        extra["actions"] = list(actions)
        subset["extra"] = extra
        return subset

    @staticmethod
    def _tool_call_bound(actions: tuple[Any, ...]) -> bool:
        """Whether withholding these actions would strand a provider tool call."""
        return any(
            isinstance(action, dict) and action.get("tool_call_id")
            for action in actions
        )

    @classmethod
    def _unexecuted_observations(
        cls,
        agent: Any,
        message: dict[str, Any],
        deferred: tuple[Any, ...],
    ) -> list[dict[str, Any]] | None:
        """Render the host's own ``not executed`` observation per withheld action.

        Pure: no action runs.  ``format_observation_messages`` pads every action it
        was not handed an output for, so passing an EMPTY output list yields exactly
        one correctly-addressed answer per deferred tool call.  Returns ``None`` when
        the host cannot be told -- the caller must then not withhold at all.
        """
        model = getattr(agent, "model", None)
        formatter = getattr(model, "format_observation_messages", None)
        if not callable(formatter):
            return None
        if not callable(getattr(agent, "add_messages", None)):
            return None
        get_template_vars = getattr(agent, "get_template_vars", None)
        try:
            template_vars = (
                get_template_vars() if callable(get_template_vars) else None
            )
            rendered = formatter(
                cls._message_with_actions(message, deferred),
                [],
                template_vars,
            )
        except Exception:  # noqa: BLE001 -- an unrenderable host is a native host
            return None
        if not rendered or len(rendered) != len(deferred):
            return None
        return list(rendered)

    @staticmethod
    def _withhold_reason_enabled() -> bool:
        """Default ON: silent withholding has no shipped-industry precedent and
        was the largest measured trajectory destroyer (run 30478454517: 2,421
        bare 'action was not executed' answers → agents misread a broken shell
        and thrashed to the step cap on 28/37 tasks). ``GT_WITHHOLD_REASON=0``
        is the kill switch back to the bare host padding."""
        return os.environ.get("GT_WITHHOLD_REASON", "1").strip().lower() not in (
            "0", "false", "no", "off"
        )

    @classmethod
    def _explain_withhold(
        cls,
        rendered: list[dict[str, Any]],
        reason_code: str,
    ) -> list[dict[str, Any]]:
        """APPEND the hold attribution to each withheld tool answer (H2).

        Never a rewrite: the host's own padding (``exception_info`` et al.)
        stays untouched, so any reader keyed on it still works. The text states
        NON-execution explicitly, attributes it to GroundTruth (not a failure),
        names the plan's reason code, and says how to proceed — the three
        properties the in-corpus positive control (briefcase: explained hold →
        immediate correct action → resolve) had and the 2,421 bare answers did
        not. Fail-closed per message: an unappendable content shape keeps the
        bare padding for that message only.
        """
        if not cls._withhold_reason_enabled():
            return rendered
        note = (
            "[GroundTruth] This action was HELD by GroundTruth, not a shell "
            f"failure ({reason_code or 'commitment_control'}): fresh staged "
            "evidence arrives in your next observation. Re-issue the command "
            "to execute it."
        )
        explained: list[dict[str, Any]] = []
        for item in rendered:
            if not isinstance(item, dict):
                explained.append(item)
                continue
            content = item.get("content")
            if isinstance(content, str):
                updated = copy(item)
                updated["content"] = f"{content}\n{note}" if content else note
                explained.append(updated)
            else:
                explained.append(item)
        return explained

    def _install(self) -> None:
        boundary = self

        def execute_actions(
            _agent: Any,
            message: dict[str, Any],
        ) -> list[dict[str, Any]]:
            actions = tuple(
                (message.get("extra") or {}).get("actions") or ()
            )
            if not actions:
                return boundary._original_execute_actions(message)
            context = boundary.context_builder(message)
            plan = decide_commitment_control(context)
            boundary._plans.append(plan)
            if plan.decision is CommitmentDecision.FRESH_INFERENCE:
                execute_now: tuple[Any, ...] = ()
            elif plan.decision is CommitmentDecision.PAUSE:
                by_id = {
                    intent.action.action_id: action
                    for intent, action in zip(context.intents, actions)
                }
                execute_now = tuple(
                    by_id[intent.action.action_id]
                    for intent in plan.execute_now
                )
            else:
                if boundary.plan_observer is not None:
                    boundary.plan_observer(context, plan, actions)
                return boundary._original_execute_actions(message)

            executed_ids = {id(action) for action in execute_now}
            deferred = tuple(
                action for action in actions if id(action) not in executed_ids
            )
            if not deferred:
                if boundary.plan_observer is not None:
                    boundary.plan_observer(context, plan, actions)
                return boundary._original_execute_actions(
                    boundary._message_with_actions(message, execute_now)
                )
            unexecuted: list[dict[str, Any]] | None = None
            if boundary._tool_call_bound(deferred):
                unexecuted = boundary._unexecuted_observations(
                    _agent, message, deferred
                )
                if unexecuted is not None:
                    unexecuted = boundary._explain_withhold(
                        unexecuted,
                        str(getattr(plan, "reason_code", "") or ""),
                    )
                if unexecuted is None:
                    # The host cannot be told the action was withheld, so
                    # withholding it would strand its tool call and end the run
                    # at the next completion.  Preserve the native batch.
                    return boundary._original_execute_actions(message)
            # Observe only an intervention that can actually be represented to
            # the host.  In particular, do not consume a one-shot interruption
            # when provider protocol forced the native fallback above.
            if boundary.plan_observer is not None:
                boundary.plan_observer(context, plan, actions)
            results: list[dict[str, Any]] = []
            if execute_now:
                results.extend(
                    boundary._original_execute_actions(
                        boundary._message_with_actions(message, execute_now)
                    )
                    or ()
                )
            if unexecuted is not None:
                results.extend(_agent.add_messages(*unexecuted) or unexecuted)
            return results

        self.agent.execute_actions = MethodType(execute_actions, self.agent)
