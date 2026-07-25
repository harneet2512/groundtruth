"""Product-owned phase and context selection policy.

This is the architecture contract for when GT may speak. Adapter surfaces can
convert their local events into these enums, but should not reimplement the
allowlist in workflow or harness-specific code.
"""
from __future__ import annotations

import os
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
    # F1 (Fable 2026-07-05): the post-edit guard/return-deletion steer. The producer
    # appends it with the literal kind "semantic_drift"; without a PayloadKind entry it was
    # absent from every PHASE_POLICY set → phase_allows() returned wrong_phase EVERY turn →
    # the steer was dead-on-arrival. Value matches the producer's literal exactly.
    SEMANTIC_DRIFT = "semantic_drift"
    LOOP_NUDGE = "detect.loop"
    STUCK_NUDGE = "l5.stuck"
    FAILURE_NUDGE = "l5.failure"
    NO_TEST_NUDGE = "l5.no_test"
    # SM-10 (2026-07-12): the typed HypothesisLedger recovery nudge. The producer appends it
    # with the literal kind "recovery" (gt_mini_patch `_recovery_candidate`). It fires on a
    # FAILURE OBSERVATION (a repeated/falsified/env failure), so it is bound to the
    # failure-observation events + allowed in the working phases an agent can be stuck in —
    # NOT ORIENT (no failure history yet) / SUBMIT (a stuck agent is not submitting).
    RECOVERY = "recovery"
    VERIFY_ADVISORY = "verify.horizon.advisory"
    VERIFY_URGENT = "verify.horizon.urgent"
    VERIFY_GATE = "verify.horizon.gate"
    VERIFY_PIVOT = "verify.horizon.pivot"


PHASE_POLICY: dict[Phase, frozenset[str]] = {
    Phase.ORIENT: frozenset({
        PayloadKind.BRIEF.value,
        PayloadKind.ORIENTATION.value,
    }),
    Phase.VIEW: frozenset({
        PayloadKind.LOCAL_EVIDENCE.value,
        # A degenerate loop is "stuck" regardless of phase — the agent can spin
        # on the same query/binary during exploration (fd stale-binary: same
        # command + identical output, no edits). The loop detector must fire in
        # VIEW, not only VERIFY (it was silent ~75 steps on the fd shape).
        PayloadKind.LOOP_NUDGE.value,
        # SM-10: an EARLY-stuck agent (no edits yet) is in VIEW/ORIENT; the recovery
        # nudge must reach it there (it is otherwise event-bound to TEST_RESULT).
        PayloadKind.RECOVERY.value,
    }),
    Phase.EDIT: frozenset({
        PayloadKind.LOCAL_EVIDENCE.value,
        PayloadKind.CONTRACT.value,
        PayloadKind.COCHANGE.value,
        PayloadKind.OBLIGATION_STATUS.value,
        PayloadKind.COHERENCE_RISK.value,
        PayloadKind.SEMANTIC_DRIFT.value,  # F1: post-edit guard/return-deletion steer
        PayloadKind.LOOP_NUDGE.value,
        PayloadKind.RECOVERY.value,  # SM-10: stuck after an edit
    }),
    Phase.VERIFY: frozenset({
        PayloadKind.OBLIGATION_STATUS.value,
        PayloadKind.STUCK_NUDGE.value,
        PayloadKind.FAILURE_NUDGE.value,
        PayloadKind.NO_TEST_NUDGE.value,
        PayloadKind.LOOP_NUDGE.value,
        PayloadKind.RECOVERY.value,  # SM-10: stuck during verification
        PayloadKind.SEMANTIC_DRIFT.value,  # F1: a drift noticed at verify still delivers
        # F4 (Fable 2026-07-05): the scope-completeness steer is PRODUCED on the review
        # predicate (edits + non-edit streak>=3), which is exactly when _detect_phase reaches
        # VERIFY. It was event-bound ONLY to REVIEW_TRANSITION, but _current_event returns
        # POST_VIEW/POST_EDIT on those turns (they outrank the review event), so the produced
        # candidate hit wrong_phase and was starved. Allowing it at VERIFY (the phase the streak
        # reaches) delivers it via the phase gate regardless of the event classification.
        PayloadKind.SCOPE_COMPLETENESS.value,
        PayloadKind.VERIFY_ADVISORY.value,
        PayloadKind.VERIFY_URGENT.value,
        PayloadKind.VERIFY_PIVOT.value,
        # A3 (Fable 2026-07-05): the verify-before-submit GATE is calibrated to fire at
        # ~7.48 action-cycles, but _detect_phase only reaches SUBMIT at >90% budget — so
        # the gate was phase-starved until it was almost too late to act on. VERIFY is the
        # phase the agent is actually verifying in; allow the gate there (kept in SUBMIT
        # too) so it delivers at its calibrated moment instead of the budget tail.
        PayloadKind.VERIFY_GATE.value,
    }),
    Phase.SUBMIT: frozenset({
        PayloadKind.OBLIGATION_STATUS.value,
        PayloadKind.VERIFY_GATE.value,
    }),
}


EVENT_BOUND_PAYLOADS: dict[Event, frozenset[str]] = {
    Event.TASK_START: frozenset({
        PayloadKind.BRIEF.value,
        PayloadKind.ORIENTATION.value,
    }),
    Event.POST_VIEW: frozenset({
        PayloadKind.LOCAL_EVIDENCE.value,
        PayloadKind.RECOVERY.value,  # SM-10: a repeated failing view/probe -> recovery
    }),
    Event.POST_EDIT: frozenset({
        PayloadKind.LOCAL_EVIDENCE.value,
        PayloadKind.CONTRACT.value,
        PayloadKind.COCHANGE.value,
        PayloadKind.COHERENCE_RISK.value,
        PayloadKind.SEMANTIC_DRIFT.value,  # F1: guard/return-deletion is a post-edit event
        PayloadKind.RECOVERY.value,  # SM-10: a failure recurring after an edit -> recovery
    }),
    Event.TEST_RESULT: frozenset({
        PayloadKind.FAILURE_NUDGE.value,
        PayloadKind.NO_TEST_NUDGE.value,
        PayloadKind.VERIFY_PIVOT.value,
        PayloadKind.RECOVERY.value,  # SM-10: a repeated/falsified test failure -> recovery
    }),
    Event.REVIEW_TRANSITION: frozenset({
        PayloadKind.SCOPE_COMPLETENESS.value,
        PayloadKind.OBLIGATION_STATUS.value,
        PayloadKind.VERIFY_ADVISORY.value,
        PayloadKind.VERIFY_URGENT.value,
        PayloadKind.VERIFY_GATE.value,
    }),
    Event.PRE_SUBMIT: frozenset({
        PayloadKind.OBLIGATION_STATUS.value,
        PayloadKind.VERIFY_GATE.value,
    }),
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
        if any(x.startswith("verify.horizon.") for x in allowed):
            return True
        # GT_VERIFY_IN_EDIT (default off => byte-identical). MEASURED on the run-27792475148
        # ledgers: verify.* candidates were 103 suppressed_wrong_phase vs 42 delivered (71%
        # dropped), incl. verify.horizon.urgent 56-suppressed / 12-delivered.
        #
        # ROOT CAUSE is circular, not incidental. `trajectory_state.derive_phase` returns VERIFY
        # only when `nonedit_streak >= 3 or test_count` — i.e. only AFTER the agent has already
        # run a test (or stalled). An agent that edits and keeps editing stays in EDIT, and
        # Phase.EDIT contains NO verify.horizon.* kind. So GT's "this edit is unverified" /
        # executed covering-RED evidence is inadmissible precisely while it is actionable, and
        # becomes admissible only once the agent has done the very thing it was meant to prompt.
        # That is also why `verify.horizon.executed` (covering_red) never lands: it is produced
        # post-edit, in EDIT phase.
        #
        # Post-edit is the correct decision boundary for edit-verification evidence. This does NOT
        # widen the dose: the <=1-dose arbiter still admits one candidate per observation, and each
        # verify producer keeps its own trigger/latch (once-per-task advisory, per-symbol covering).
        # Scoped to EDIT only — ORIENT/VIEW have no edit to verify, so they stay closed.
        if phase is Phase.EDIT and os.environ.get("GT_VERIFY_IN_EDIT", "0").strip() == "1":
            return True
    # GT_SCOPE_AT_SEARCH (default off => byte-identical). Same defect class, different feature.
    # `consensus.scope` (def_partition — one of the 17 DIRECT) is admissible ONLY in VERIFY, but it
    # FIRES on a search/view that resolves a symbol's definition and partition, which happens in
    # VIEW/EDIT. Since VERIFY needs `test_count or nonedit_streak>=3`, def_partition answers the
    # agent's SEARCH only after the agent has run a TEST. Measured: 34 suppressed_wrong_phase vs 7
    # delivered (83% lost). Admitting it where it fires is what makes it a search-time answer at all.
    if (k == "consensus.scope" and phase in (Phase.VIEW, Phase.EDIT)
            and os.environ.get("GT_SCOPE_AT_SEARCH", "0").strip() == "1"):
        return True
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
