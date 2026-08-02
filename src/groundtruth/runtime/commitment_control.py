"""Pure commitment-control policy for the canonical GroundTruth runtime.

The module classifies structured action intents by assurance, preserves action
order, and decides whether a mixed action batch may continue, must stop after
its epistemic prefix, or requires a fresh inference before a commitment.

It does not execute tools, schedule evidence, render capsules, or authorize a
native action.  In particular, ``BLOCK_CERTIFICATE`` withholds only a clean GT
completion certificate; the host harness remains the authority for whether a
native terminal action may proceed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from .reasoning_runtime import (
    ActionOperation,
    CanonicalAction,
    EvidenceGrade,
    EvidenceLifecycle,
    FailurePolicyState,
    RuntimeHealthState,
)


class ActionAssuranceClass(str, Enum):
    EPISTEMIC = "EPISTEMIC"
    REVERSIBLE_COMMITMENT = "REVERSIBLE_COMMITMENT"
    HIGH_IMPACT_COMMITMENT = "HIGH_IMPACT_COMMITMENT"
    TERMINAL_COMMITMENT = "TERMINAL_COMMITMENT"


class BatchPhase(str, Enum):
    BEFORE_BATCH = "BEFORE_BATCH"
    AFTER_EPISTEMIC_PREFIX = "AFTER_EPISTEMIC_PREFIX"


class CommitmentDecision(str, Enum):
    ALLOW = "ALLOW"
    PAUSE = "PAUSE"
    FRESH_INFERENCE = "FRESH_INFERENCE"
    UNASSURED = "UNASSURED"
    BLOCK_CERTIFICATE = "BLOCK_CERTIFICATE"


_EPISTEMIC_OPERATIONS = frozenset(
    {
        ActionOperation.SEARCH,
        ActionOperation.VIEW_SOURCE,
        ActionOperation.VIEW_SYMBOL,
        ActionOperation.TEST,
        ActionOperation.COMPILE,
    }
)

_INTRINSICALLY_HIGH_IMPACT_OPERATIONS = frozenset(
    {
        ActionOperation.SIGNATURE_CHANGE,
        ActionOperation.FILE_DELETE,
        ActionOperation.FILE_RENAME,
    }
)

_REVERSIBLE_OPERATIONS = frozenset(
    {
        ActionOperation.EDIT,
        ActionOperation.FILE_CREATE,
    }
)

_EVIDENCE_LIFECYCLES_ELIGIBLE_FOR_INTERVENTION = frozenset(
    {
        EvidenceLifecycle.READY,
        EvidenceLifecycle.RELEASED,
        EvidenceLifecycle.DELIVERED,
        EvidenceLifecycle.ACTIVE,
    }
)


@dataclass(frozen=True)
class CommitmentIntent:
    """One structured action plus host-known assurance facts.

    ``sandboxed`` describes the action boundary, not a guess made from command
    text.  Explicit high-impact facts dominate sandbox reversibility.
    """

    action: CanonicalAction
    sandboxed: bool = False
    external_side_effect: bool = False
    destructive: bool = False
    public_contract_change: bool = False
    terminal: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "sandboxed",
            "external_side_effect",
            "destructive",
            "public_contract_change",
            "terminal",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be bool")


@dataclass(frozen=True)
class CommitmentEvidence:
    """Scheduler-approved evidence relevant to a pending commitment.

    Materiality is explicit and action-scoped.  The commitment controller does
    not infer it from feature names, text similarity, or carrier contents.
    """

    evidence_id: str
    decision_id: str
    grade: EvidenceGrade
    lifecycle: EvidenceLifecycle
    fresh: bool
    superseded: bool
    release_allowed: bool
    visible_to_model_call_ids: tuple[str, ...]
    material_action_ids: tuple[str, ...]
    # A commitment may be deferred only when these exact bytes are already
    # staged at the provider boundary for the next inference.  Eligibility in
    # the evidence store alone is not a delivery guarantee.
    staged_for_next_inference: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "visible_to_model_call_ids",
            tuple(self.visible_to_model_call_ids),
        )
        object.__setattr__(
            self,
            "material_action_ids",
            tuple(self.material_action_ids),
        )
        if not self.evidence_id or not self.decision_id:
            raise ValueError("evidence_id and decision_id are required")
        if not self.material_action_ids:
            raise ValueError("material_action_ids must be non-empty")
        if len(set(self.material_action_ids)) != len(
            self.material_action_ids
        ):
            raise ValueError("material_action_ids must be unique")
        if len(set(self.visible_to_model_call_ids)) != len(
            self.visible_to_model_call_ids
        ):
            raise ValueError("visible_to_model_call_ids must be unique")
        for field_name in (
            "fresh",
            "superseded",
            "release_allowed",
            "staged_for_next_inference",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be bool")


@dataclass(frozen=True)
class CommitmentControlContext:
    intents: tuple[CommitmentIntent, ...]
    phase: BatchPhase
    active_decision_id: str
    proposing_model_call_id: str
    evidence: tuple[CommitmentEvidence, ...]
    failure_state: FailurePolicyState
    epistemic_prefix_may_change_decision: bool
    certificate_requirements_met: bool
    repository_revision: str = ""
    consumed_interruption_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intents", tuple(self.intents))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(
            self,
            "consumed_interruption_keys",
            tuple(self.consumed_interruption_keys),
        )
        if not self.intents:
            raise ValueError("intents must be non-empty")
        action_ids = tuple(intent.action.action_id for intent in self.intents)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("action identities must be unique within a batch")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError(
                "evidence identities must be unique within a decision"
            )
        if not self.active_decision_id:
            raise ValueError("active_decision_id is required")
        if not self.proposing_model_call_id:
            raise ValueError("proposing_model_call_id is required")
        if (
            self.failure_state.attempt_id == ""
            or not self.failure_state.native_path_enabled
        ):
            raise ValueError(
                "commitment control requires an enabled native action path"
            )
        if type(self.epistemic_prefix_may_change_decision) is not bool:
            raise TypeError(
                "epistemic_prefix_may_change_decision must be bool"
            )
        if type(self.certificate_requirements_met) is not bool:
            raise TypeError("certificate_requirements_met must be bool")
        if len(set(self.consumed_interruption_keys)) != len(
            self.consumed_interruption_keys
        ):
            raise ValueError("consumed_interruption_keys must be unique")


@dataclass(frozen=True)
class CommitmentControlPlan:
    decision: CommitmentDecision
    execute_now: tuple[CommitmentIntent, ...]
    deferred: tuple[CommitmentIntent, ...]
    epistemic_prefix: tuple[CommitmentIntent, ...]
    qualifying_evidence_ids: tuple[str, ...]
    fresh_inference_required: bool
    gt_certificate_allowed: bool
    native_path_preserved: bool
    reason_code: str
    interruption_key: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "execute_now",
            "deferred",
            "epistemic_prefix",
            "qualifying_evidence_ids",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if not self.reason_code:
            raise ValueError("reason_code is required")
        if not self.native_path_preserved:
            raise ValueError("native action path must remain preserved")
        execute_ids = tuple(
            intent.action.action_id for intent in self.execute_now
        )
        deferred_ids = tuple(
            intent.action.action_id for intent in self.deferred
        )
        if set(execute_ids).intersection(deferred_ids):
            raise ValueError("execute_now and deferred must be disjoint")
        if self.decision is CommitmentDecision.PAUSE and (
            not self.epistemic_prefix
            or self.execute_now != self.epistemic_prefix
            or not self.deferred
        ):
            raise ValueError(
                "PAUSE must execute a non-empty epistemic prefix and defer work"
            )
        if self.decision is CommitmentDecision.FRESH_INFERENCE and (
            self.execute_now
            or not self.deferred
            or not self.qualifying_evidence_ids
            or not self.fresh_inference_required
        ):
            raise ValueError(
                "FRESH_INFERENCE requires qualifying evidence and deferred work"
            )
        if (
            self.decision is not CommitmentDecision.FRESH_INFERENCE
            and self.fresh_inference_required
        ):
            raise ValueError(
                "fresh_inference_required contradicts commitment decision"
            )
        if (
            self.decision is not CommitmentDecision.ALLOW
            and self.gt_certificate_allowed
        ):
            raise ValueError(
                "only an ALLOW decision may permit GT certification"
            )


def classify_action(intent: CommitmentIntent) -> ActionAssuranceClass:
    """Classify one action from structured operation and assurance facts."""

    operation = intent.action.operation
    if intent.terminal or operation is ActionOperation.SUBMIT:
        return ActionAssuranceClass.TERMINAL_COMMITMENT
    if (
        intent.external_side_effect
        or intent.destructive
        or intent.public_contract_change
        or operation in _INTRINSICALLY_HIGH_IMPACT_OPERATIONS
    ):
        return ActionAssuranceClass.HIGH_IMPACT_COMMITMENT
    if operation in _EPISTEMIC_OPERATIONS:
        return ActionAssuranceClass.EPISTEMIC
    if operation in _REVERSIBLE_OPERATIONS and intent.sandboxed:
        return ActionAssuranceClass.REVERSIBLE_COMMITMENT
    return ActionAssuranceClass.HIGH_IMPACT_COMMITMENT


def _epistemic_prefix(
    intents: tuple[CommitmentIntent, ...],
) -> tuple[CommitmentIntent, ...]:
    prefix: list[CommitmentIntent] = []
    for intent in intents:
        if classify_action(intent) is not ActionAssuranceClass.EPISTEMIC:
            break
        prefix.append(intent)
    return tuple(prefix)


def _has_terminal(intents: tuple[CommitmentIntent, ...]) -> bool:
    return any(
        classify_action(intent)
        is ActionAssuranceClass.TERMINAL_COMMITMENT
        for intent in intents
    )


def _qualifying_evidence(
    context: CommitmentControlContext,
) -> tuple[CommitmentEvidence, ...]:
    commitment_action_ids = {
        intent.action.action_id
        for intent in context.intents
        if classify_action(intent) is not ActionAssuranceClass.EPISTEMIC
    }
    return tuple(
        sorted(
            (
                evidence
                for evidence in context.evidence
                if evidence.decision_id == context.active_decision_id
                and evidence.grade is EvidenceGrade.VERIFIED
                and evidence.fresh
                and not evidence.superseded
                and evidence.release_allowed
                and evidence.staged_for_next_inference
                and evidence.lifecycle
                in _EVIDENCE_LIFECYCLES_ELIGIBLE_FOR_INTERVENTION
                and context.proposing_model_call_id
                not in evidence.visible_to_model_call_ids
                and bool(
                    commitment_action_ids.intersection(
                        evidence.material_action_ids
                    )
                )
            ),
            key=lambda item: item.evidence_id,
        )
    )


def _plan(
    *,
    decision: CommitmentDecision,
    execute_now: tuple[CommitmentIntent, ...],
    deferred: tuple[CommitmentIntent, ...] = (),
    epistemic_prefix: tuple[CommitmentIntent, ...] = (),
    qualifying_evidence_ids: tuple[str, ...] = (),
    fresh_inference_required: bool = False,
    gt_certificate_allowed: bool = False,
    reason_code: str,
    interruption_key: str = "",
) -> CommitmentControlPlan:
    return CommitmentControlPlan(
        decision=decision,
        execute_now=execute_now,
        deferred=deferred,
        epistemic_prefix=epistemic_prefix,
        qualifying_evidence_ids=qualifying_evidence_ids,
        fresh_inference_required=fresh_inference_required,
        gt_certificate_allowed=gt_certificate_allowed,
        native_path_preserved=True,
        reason_code=reason_code,
        interruption_key=interruption_key,
    )


def _interruption_key(
    context: CommitmentControlContext,
    qualifying: tuple[CommitmentEvidence, ...],
) -> str:
    """Identify one logical intervention independently of host action ids."""

    commitments = tuple(
        {
            "operation": intent.action.operation.value,
            "structured_operation": intent.action.structured_operation,
            "subject": intent.action.subject,
            "targets": list(intent.action.targets),
        }
        for intent in context.intents
        if classify_action(intent) is not ActionAssuranceClass.EPISTEMIC
    )
    identity = {
        "decision_id": context.active_decision_id,
        "repository_revision": context.repository_revision,
        "commitments": commitments,
        "evidence_ids": [item.evidence_id for item in qualifying],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decide_commitment_control(
    context: CommitmentControlContext,
) -> CommitmentControlPlan:
    """Return one deterministic commitment-control decision.

    The decision order is deliberate:

    1. An unavailable or quarantined GT never interrupts the native path.
    2. Epistemic actions are never withheld merely because a later commitment
       exists in the same native batch.
    3. Only scheduler-approved, verified, fresh, unseen, staged, and
       action-material
       evidence can require a fresh inference.  Existing evidence can
       intervene before a single commitment; a mixed batch is re-evaluated
       after its epistemic prefix.
    4. Terminal work may proceed natively, but GT certification is withheld
       whenever canonical assurance is absent or incomplete.
    """

    intents = context.intents
    terminal = _has_terminal(intents)
    unavailable = (
        context.failure_state.health is RuntimeHealthState.QUARANTINED
        or not context.failure_state.gt_emission_enabled
        or not context.failure_state.gt_interruption_enabled
    )
    if unavailable:
        if terminal:
            return _plan(
                decision=CommitmentDecision.BLOCK_CERTIFICATE,
                execute_now=intents,
                reason_code="GT_UNAVAILABLE_TERMINAL_UNCERTIFIED",
            )
        return _plan(
            decision=CommitmentDecision.UNASSURED,
            execute_now=intents,
            reason_code="GT_UNAVAILABLE_NATIVE_PATH",
        )

    qualifying = _qualifying_evidence(context)
    if qualifying:
        interruption_key = _interruption_key(context, qualifying)
        if interruption_key in context.consumed_interruption_keys:
            return _plan(
                decision=CommitmentDecision.ALLOW,
                execute_now=intents,
                gt_certificate_allowed=(
                    context.failure_state.gt_certification_enabled
                    and context.certificate_requirements_met
                ),
                reason_code="INTERRUPTION_ALREADY_CONSUMED",
                interruption_key=interruption_key,
            )
        return _plan(
            decision=CommitmentDecision.FRESH_INFERENCE,
            execute_now=(),
            deferred=intents,
            qualifying_evidence_ids=tuple(
                item.evidence_id for item in qualifying
            ),
            fresh_inference_required=True,
            reason_code="VERIFIED_UNSEEN_MATERIAL_EVIDENCE",
            interruption_key=interruption_key,
        )

    certificate_allowed = (
        context.failure_state.gt_certification_enabled
        and context.certificate_requirements_met
    )
    if terminal and not certificate_allowed:
        return _plan(
            decision=CommitmentDecision.BLOCK_CERTIFICATE,
            execute_now=intents,
            reason_code="TERMINAL_ASSURANCE_INCOMPLETE",
        )

    return _plan(
        decision=CommitmentDecision.ALLOW,
        execute_now=intents,
        gt_certificate_allowed=certificate_allowed,
        reason_code="NORMAL_POLICY_ALLOW",
    )
