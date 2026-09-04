"""Product-owned phase and context selection policy.

This is the architecture contract for when GT may speak. Adapter surfaces can
convert their local events into these enums, but should not reimplement the
allowlist in workflow or harness-specific code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet


POLICY_VERSION = "gt.runtime.context_policy.v1"


class Phase(Enum):
    ORIENT = "orient"
    VIEW = "view"
    EDIT = "edit"
    VERIFY = "verify"
    SUBMIT = "submit"


class Event(Enum):
    TASK_START = "task_start"
    POST_VIEW = "post_view"
    POST_EDIT = "post_edit"
    TEST_RESULT = "test_result"
    REVIEW_TRANSITION = "review_transition"
    PRE_SUBMIT = "pre_submit"


class PayloadKind(Enum):
    BRIEF = "brief"
    ORIENTATION = "orientation"
    SCOPE_COMPLETENESS = "consensus.scope"
    LOCAL_EVIDENCE = "l3b.evidence"
    CONTRACT = "l3.contract"
    COCHANGE = "l3.cochange"
    OBLIGATION_STATUS = "spec.obligation"
    COHERENCE_RISK = "detect.coherence"
    LOOP_NUDGE = "detect.loop"
    STUCK_NUDGE = "l5.stuck"
    FAILURE_NUDGE = "l5.failure"
    NO_TEST_NUDGE = "l5.no_test"
    VERIFY_ADVISORY = "verify.horizon.advisory"
    VERIFY_URGENT = "verify.horizon.urgent"
    VERIFY_GATE = "verify.horizon.gate"
    VERIFY_PIVOT = "verify.horizon.pivot"


PHASE_POLICY: dict[Phase, frozenset[str]] = {
    Phase.ORIENT: frozenset(
        {
            PayloadKind.BRIEF.value,
            PayloadKind.ORIENTATION.value,
        }
    ),
    Phase.VIEW: frozenset(
        {
            PayloadKind.LOCAL_EVIDENCE.value,
            # A degenerate loop is "stuck" regardless of phase — the agent can spin
            # on the same query/binary during exploration (fd stale-binary: same
            # command + identical output, no edits). The loop detector must fire in
            # VIEW, not only VERIFY (it was silent ~75 steps on the fd shape).
            PayloadKind.LOOP_NUDGE.value,
        }
    ),
    Phase.EDIT: frozenset(
        {
            PayloadKind.LOCAL_EVIDENCE.value,
            PayloadKind.CONTRACT.value,
            PayloadKind.COCHANGE.value,
            PayloadKind.OBLIGATION_STATUS.value,
            PayloadKind.COHERENCE_RISK.value,
            PayloadKind.LOOP_NUDGE.value,
        }
    ),
    Phase.VERIFY: frozenset(
        {
            PayloadKind.OBLIGATION_STATUS.value,
            PayloadKind.STUCK_NUDGE.value,
            PayloadKind.FAILURE_NUDGE.value,
            PayloadKind.NO_TEST_NUDGE.value,
            PayloadKind.LOOP_NUDGE.value,
            PayloadKind.VERIFY_ADVISORY.value,
            PayloadKind.VERIFY_URGENT.value,
            PayloadKind.VERIFY_PIVOT.value,
        }
    ),
    Phase.SUBMIT: frozenset(
        {
            PayloadKind.OBLIGATION_STATUS.value,
            PayloadKind.VERIFY_GATE.value,
        }
    ),
}


EVENT_BOUND_PAYLOADS: dict[Event, frozenset[str]] = {
    Event.TASK_START: frozenset(
        {
            PayloadKind.BRIEF.value,
            PayloadKind.ORIENTATION.value,
        }
    ),
    Event.POST_VIEW: frozenset(
        {
            PayloadKind.LOCAL_EVIDENCE.value,
        }
    ),
    Event.POST_EDIT: frozenset(
        {
            PayloadKind.LOCAL_EVIDENCE.value,
            PayloadKind.CONTRACT.value,
            PayloadKind.COCHANGE.value,
            PayloadKind.COHERENCE_RISK.value,
        }
    ),
    Event.TEST_RESULT: frozenset(
        {
            PayloadKind.FAILURE_NUDGE.value,
            PayloadKind.NO_TEST_NUDGE.value,
            PayloadKind.VERIFY_PIVOT.value,
        }
    ),
    Event.REVIEW_TRANSITION: frozenset(
        {
            PayloadKind.SCOPE_COMPLETENESS.value,
            PayloadKind.OBLIGATION_STATUS.value,
            PayloadKind.VERIFY_ADVISORY.value,
            PayloadKind.VERIFY_URGENT.value,
            PayloadKind.VERIFY_GATE.value,
        }
    ),
    Event.PRE_SUBMIT: frozenset(
        {
            PayloadKind.OBLIGATION_STATUS.value,
            PayloadKind.VERIFY_GATE.value,
        }
    ),
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def normalize_kind(kind: str | PayloadKind) -> str:
    if isinstance(kind, PayloadKind):
        return kind.value
    return str(kind or "")


def allowed_payloads(phase: Phase, event: Event | None = None) -> FrozenSet[str]:
    allowed = set(PHASE_POLICY.get(phase, frozenset()))
    if event is not None:
        allowed |= set(EVENT_BOUND_PAYLOADS.get(event, frozenset()))
    return frozenset(allowed)


def phase_allows(
    kind: str | PayloadKind,
    phase: Phase,
    policy: dict[Phase, FrozenSet[str]] | None = None,
) -> bool:
    k = normalize_kind(kind)
    allowed = (policy or PHASE_POLICY).get(phase, frozenset())
    if k in allowed:
        return True
    if k.startswith("verify.horizon."):
        return any(x.startswith("verify.horizon.") for x in allowed)
    return False


def should_emit(
    kind: str | PayloadKind,
    phase: Phase,
    *,
    event: Event | None = None,
    event_bound: bool = False,
) -> PolicyDecision:
    k = normalize_kind(kind)
    if event_bound and event is not None:
        if k in EVENT_BOUND_PAYLOADS.get(event, frozenset()):
            return PolicyDecision(True, "event_bound")
    if phase_allows(k, phase):
        return PolicyDecision(True, "phase_allowed")
    return PolicyDecision(False, "wrong_phase")
