"""Mini-SWE batch boundary for canonical commitment control.

The boundary never parses evidence, renders GT text, or executes a deferred
commitment.  It applies a pure :class:`CommitmentControlPlan` to the host
agent's existing ``execute_actions`` call:

* ALLOW/UNASSURED/BLOCK_CERTIFICATE: preserve the native batch unchanged;
* PAUSE: execute only the original contiguous epistemic prefix;
* FRESH_INFERENCE: execute nothing so the run loop samples again with the
  already-staged canonical capsule.
"""

from __future__ import annotations

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
            if boundary.plan_observer is not None:
                boundary.plan_observer(context, plan, actions)
            if plan.decision is CommitmentDecision.FRESH_INFERENCE:
                return []
            if plan.decision is CommitmentDecision.PAUSE:
                by_id = {
                    intent.action.action_id: action
                    for intent, action in zip(context.intents, actions)
                }
                prefix_actions = tuple(
                    by_id[intent.action.action_id]
                    for intent in plan.execute_now
                )
                return boundary._original_execute_actions(
                    boundary._message_with_actions(message, prefix_actions)
                )
            return boundary._original_execute_actions(message)

        self.agent.execute_actions = MethodType(execute_actions, self.agent)
