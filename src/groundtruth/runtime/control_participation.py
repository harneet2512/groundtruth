"""Typed, render-neutral participation records for CAP decision controls.

Unlike :class:`feature_lineage.DeliveryLineage`, a participation record never
claims ownership of evidence bytes.  It says only that one control's executable
decision function was reached for a particular candidate/context.  The schema is
role-generic and declares terminal contracts for every eligibility and mediator
CAP control.  Byte owners remain in delivery lineage, where ownership belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from .evidence_envelope import (
    ObservationBinding,
    observation_binding_to_dict,
    validate_observation_binding,
)
from .fact_registry import is_registered
from .feature_lineage import (
    CAP_ELIGIBILITY_IDS,
    CAP_FEATURE_IDS,
    CAP_MEDIATOR_IDS,
    cap_role_for,
)


CONTROL_PARTICIPATION_SCHEMA = "gt.control_participation.v1"
PARTICIPATION_DECISIONS = frozenset(
    {"APPLIED", "NO_EFFECT", "SUPPRESSED", "ERROR"}
)
CONTROL_PRECEDES_DELIVERY = "CONTROL_PRECEDES_DELIVERY"
RECEIPT_FOLLOWS_DELIVERY = "RECEIPT_FOLLOWS_DELIVERY"
PARTICIPATION_TEMPORAL_RELATIONS = frozenset({
    CONTROL_PRECEDES_DELIVERY,
    RECEIPT_FOLLOWS_DELIVERY,
})


@dataclass(frozen=True)
class ControlDecisionContract:
    feature_id: str
    role: str
    decision_sites: tuple[str, ...]
    measurement_status: str
    fact_class_required: bool = False
    unmeasured_reason: str = ""
    temporal_relation: str = CONTROL_PRECEDES_DELIVERY

    def __post_init__(self) -> None:
        if self.feature_id not in CAP_FEATURE_IDS:
            raise ValueError(f"unknown CAP feature: {self.feature_id!r}")
        if self.role != cap_role_for(self.feature_id):
            raise ValueError(f"CAP role mismatch for {self.feature_id}")
        if self.measurement_status not in {"SUPPORTED", "UNMEASURED"}:
            raise ValueError("unknown measurement_status")
        if self.measurement_status == "SUPPORTED" and not self.decision_sites:
            raise ValueError("SUPPORTED control requires an executable decision site")
        if self.measurement_status == "UNMEASURED" and not self.unmeasured_reason:
            raise ValueError("UNMEASURED control requires a reason")
        if not isinstance(self.fact_class_required, bool):
            raise ValueError("fact_class_required must be a bool")
        if self.temporal_relation not in PARTICIPATION_TEMPORAL_RELATIONS:
            raise ValueError("unknown temporal_relation")
        if (
            self.temporal_relation == RECEIPT_FOLLOWS_DELIVERY
            and self.role != "mediator"
        ):
            raise ValueError("post-delivery receipt controls must be mediators")
        if self.role == "mediator" and not self.fact_class_required:
            raise ValueError("mediator controls require concrete FACT identity")


def _eligibility(
    feature_id: str, *sites: str, unmeasured_reason: str = "",
) -> ControlDecisionContract:
    return ControlDecisionContract(
        feature_id=feature_id,
        role="eligibility",
        decision_sites=tuple(sites),
        measurement_status="SUPPORTED" if sites else "UNMEASURED",
        unmeasured_reason=unmeasured_reason,
    )


def _mediator(
    feature_id: str,
    *sites: str,
    temporal_relation: str = CONTROL_PRECEDES_DELIVERY,
) -> ControlDecisionContract:
    return ControlDecisionContract(
        feature_id=feature_id,
        role="mediator",
        decision_sites=tuple(sites),
        measurement_status="SUPPORTED",
        fact_class_required=True,
        temporal_relation=temporal_relation,
    )


# This is a terminal-contract map, not an enabled-feature inventory.  A supported
# entry identifies the exact function where the control decides.  An unmeasured
# entry records why the owned mini-seam cannot honestly witness its terminal effect.
CONTROL_DECISION_CONTRACTS = {
    "GT_OBLIGATION_FRESHNESS": _eligibility(
        "GT_OBLIGATION_FRESHNESS",
        "mini_seam.obligation_nudge.freshness_demotion",
    ),
    "GT_SS_NOVELTY": _eligibility(
        "GT_SS_NOVELTY", "mini_seam.ss_content_decision.novelty",
    ),
    "GT_SS_EXEC_TRUTH": _eligibility(
        "GT_SS_EXEC_TRUTH", "mini_seam.covering_selection.runner_eligibility",
    ),
    "GT_EDIT_OVERLAY": _eligibility(
        "GT_EDIT_OVERLAY",
        "gateway.route_delivery.episode_overlay",
    ),
    "GT_REGISTRY_ENFORCE": _eligibility(
        "GT_REGISTRY_ENFORCE",
        "gateway.augment.renderer_enforcement",
        "gateway.route_delivery.registry_timing",
    ),
    "GT_SS_RECOVERY_V2": _eligibility(
        "GT_SS_RECOVERY_V2", "mini_seam.recovery_candidate.v2_gate",
    ),
    "GT_BRIEF_MINIMAL": _eligibility(
        "GT_BRIEF_MINIMAL",
        "pretask.v1r_brief.minimal_reduction",
    ),
    "GT_SS_DEDUP2": _eligibility(
        "GT_SS_DEDUP2", "mini_seam.ss_content_decision.semantic_dedup",
    ),
    "GT_SS_ELIGIBILITY": _eligibility(
        "GT_SS_ELIGIBILITY", "mini_seam.cd_prefix.command_substitution",
    ),
    # ITEM 0 (2026-07-18): the post_search lattice MASTER enable gate. Its executable decision
    # site is the seam's _search_localize_decision master check (gt_mini_patch _POST_SEARCH_ON,
    # gt_mini_patch.py:4588) that decides WHETHER the def-partition producer runs on the agent's
    # own grep — the same lattice GT_SS_ELIGIBILITY widens.
    "GT_POST_SEARCH": _eligibility(
        "GT_POST_SEARCH", "mini_seam.post_search.lattice_master_enable",
    ),
    "GT_XSESSION_MEMORY": _eligibility(
        "GT_XSESSION_MEMORY",
        "gateway.xsession_policy.inert_suppression",
    ),
    "GT_D7_RELATEDNESS": _eligibility(
        "GT_D7_RELATEDNESS", "mini_seam.receipt_judge.relatedness",
    ),
    "GT_SS_SHADOW": _eligibility(
        "GT_SS_SHADOW", "mini_seam.shadow_holdout.assignment",
    ),
    "GT_SS_LATE_DROP": _eligibility(
        "GT_SS_LATE_DROP", "mini_seam.ss_content_decision.late_drop",
    ),
    "GT_BRIEF_NATIVE": _mediator(
        "GT_BRIEF_NATIVE", "pretask.v1r_brief.native_render",
    ),
    "GT_COMPLETION_CERT": _mediator(
        "GT_COMPLETION_CERT", "mini_seam.submit_gate.completion_certificate",
    ),
    "GT_CONTENT_LEG": _mediator(
        "GT_CONTENT_LEG", "pretask.v1r_brief.content_bm25_rank",
    ),
    "GT_CONTRACT_BILATERAL": _mediator(
        "GT_CONTRACT_BILATERAL", "mini_seam.contract.bilateral_selection",
        "gateway.caller_contract.bilateral_selection",
    ),
    "GT_CONTRACT_MODE": _mediator(
        "GT_CONTRACT_MODE", "mini_seam.contract.mode_selection",
        "gateway.caller_contract.mode_selection",
    ),
    "GT_CONTRACT_NATIVE": _mediator(
        "GT_CONTRACT_NATIVE", "mini_seam.contract.native_render",
        "gateway.caller_contract.native_render",
    ),
    "GT_EVIDENCE_NATIVE": _mediator(
        "GT_EVIDENCE_NATIVE", "mini_seam.evidence.native_render",
        "gateway.caller_contract.caller_rows",
    ),
    "GT_GATEWAY": _mediator(
        "GT_GATEWAY", "gateway.augment.candidate_admission",
        # The admission site stamps the PRE-render candidate identity (eligibility /
        # decision chronology: the candidate was admitted BEFORE delivery). It can never
        # join a delivery, whose sealed bytes are the FINAL rendered form. The committed
        # site carries the seam's committed-delivery identity so the exact mediation join
        # closes (B-GW, 2026-07-16) — mirroring GT_GATEWAY_NATIVE.
        "mini_seam.gateway.candidate_committed",
    ),
    "GT_GATEWAY_EDIT_BRIDGES": _mediator(
        "GT_GATEWAY_EDIT_BRIDGES", "gateway.augment.edit_bridge_candidate",
    ),
    "GT_GATEWAY_NATIVE": _mediator(
        "GT_GATEWAY_NATIVE", "mini_seam.gateway.native_render",
    ),
    "GT_GLOBAL_ARBITER": _mediator(
        "GT_GLOBAL_ARBITER", "mini_seam.global_arbiter.winner_selection",
    ),
    "GT_INSEAM_METRICS": _mediator(
        "GT_INSEAM_METRICS", "mini_seam.inseam_metrics.fact_observation",
    ),
    "GT_L6_FRESH": _mediator(
        "GT_L6_FRESH", "mini_seam.graph_db.fresh_copy_selection",
    ),
    "GT_LANE_ENVELOPE": _mediator(
        "GT_LANE_ENVELOPE", "mini_seam.lane_envelope.candidate_conversion",
    ),
    "GT_NUDGE_NATIVE": _mediator(
        "GT_NUDGE_NATIVE", "mini_seam.nudge.native_render",
    ),
    "GT_POST_SEARCH_NATIVE": _mediator(
        "GT_POST_SEARCH_NATIVE", "mini_seam.post_search.native_render",
    ),
    "GT_SCOPE_NATIVE": _mediator(
        "GT_SCOPE_NATIVE", "mini_seam.scope.native_render",
    ),
    "GT_SEM_BODY": _mediator(
        "GT_SEM_BODY", "pretask.v1r_brief.semantic_body_rank",
    ),
    "GT_SS_ACK_FORM": _mediator(
        "GT_SS_ACK_FORM",
        # form_selection is the model-visible preamble swap in gt_agent (pre-agent,
        # outside the runtime ledger). It is declared and currently UNWRITTEN — an
        # honest dark site awaiting a pre-agent sidecar writer, not deleted to fit.
        "mini_seam.acknowledgment.form_selection",
        "pretask.v1r_brief.ack_requirement_filter",
    ),
    "GT_SS_ACK_METRICS": _mediator(
        "GT_SS_ACK_METRICS",
        "mini_seam.acknowledgment.receipt_grading",
        temporal_relation=RECEIPT_FOLLOWS_DELIVERY,
    ),
    "GT_SS_ARBITER_V2": _mediator(
        "GT_SS_ARBITER_V2",
        "gateway.augment.empty_payload_guard",
        "mini_seam.global_arbiter.v2_selection",
    ),
    # P4 (B-TERM 2026-07-16): GT_SS_COHERENCE_V2 was reclassified byte_owner → mediator
    # (feature_lineage CAP_BYTE_OWNER_IDS). It mediates the ``recovery`` FACT class: the
    # coherence-collapse detector shapes the recovery/pivot nudge (fact_registry aliases
    # ``coherence_collapse`` → ``recovery`` and authorizes producer ``ss_coherence_v2`` for it).
    # The decision site below is declared and currently UNWRITTEN in the seam — an honest dark
    # site awaiting a ``_control_participation_record`` emitter at the coherence gate (mirrors the
    # GT_SS_ACK_FORM.form_selection precedent). Until then the control terminal reads no rows and
    # reports UNMEASURED (fail-closed) — never a fabricated effect.
    "GT_SS_COHERENCE_V2": _mediator(
        "GT_SS_COHERENCE_V2", "mini_seam.coherence.recovery_pivot",
    ),
    "GT_SS_PROVENANCE": _mediator(
        "GT_SS_PROVENANCE", "mini_seam.delivery.provenance_seal",
    ),
    "GT_STEER_NATIVE": _mediator(
        "GT_STEER_NATIVE", "mini_seam.steer.native_render",
    ),
    "GT_VERIFICATION_PLAN": _mediator(
        "GT_VERIFICATION_PLAN", "mini_seam.verification.plan_selection",
    ),
    "GT_VERIFY_EXECUTE": _mediator(
        "GT_VERIFY_EXECUTE", "mini_seam.verification.execution",
    ),
    "GT_XSESSION_RANKUP": _mediator(
        "GT_XSESSION_RANKUP", "gateway.xsession_rankup.boost",
    ),
}

if set(CONTROL_DECISION_CONTRACTS) != set(CAP_ELIGIBILITY_IDS | CAP_MEDIATOR_IDS):
    raise ValueError("control decision-contract inventory drift")


def control_contract(feature_id: str) -> ControlDecisionContract:
    try:
        return CONTROL_DECISION_CONTRACTS[feature_id]
    except KeyError as exc:
        raise ValueError(f"no control decision contract: {feature_id!r}") from exc


@dataclass(frozen=True)
class ControlParticipation:
    schema: str
    feature_id: str
    role: str
    decision_site: str
    decision: str
    iteration: int
    candidate_chars: int
    candidate_sha256_16: str
    fact_class: str | None
    reason: str
    candidate_id: str = ""
    temporal_relation: str = CONTROL_PRECEDES_DELIVERY
    related_delivery_iteration: int | None = None
    observation_binding: ObservationBinding | None = None

    def __post_init__(self) -> None:
        if self.schema != CONTROL_PARTICIPATION_SCHEMA:
            raise ValueError(f"unsupported schema: {self.schema!r}")
        if self.feature_id not in CAP_FEATURE_IDS:
            raise ValueError(f"unknown CAP feature: {self.feature_id!r}")
        if self.role != cap_role_for(self.feature_id):
            raise ValueError(f"CAP role mismatch for {self.feature_id}")
        contract = control_contract(self.feature_id)
        if contract.measurement_status != "SUPPORTED":
            raise ValueError(
                f"{self.feature_id} is UNMEASURED: {contract.unmeasured_reason}"
            )
        if self.decision_site not in contract.decision_sites:
            raise ValueError(
                f"unsupported decision site for {self.feature_id}: "
                f"{self.decision_site!r}"
            )
        if self.temporal_relation != contract.temporal_relation:
            raise ValueError(
                f"temporal relation mismatch for {self.feature_id}: "
                f"{self.temporal_relation!r}"
            )
        if self.decision not in PARTICIPATION_DECISIONS:
            raise ValueError(f"unknown participation decision: {self.decision!r}")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be a string")
        if self.decision in {"ERROR", "SUPPRESSED"} and not self.reason:
            raise ValueError(f"{self.decision} requires a non-empty reason")
        if type(self.iteration) is not int or self.iteration < 0:
            raise ValueError("iteration must be a non-negative integer")
        if self.temporal_relation == RECEIPT_FOLLOWS_DELIVERY:
            if (
                type(self.related_delivery_iteration) is not int
                or self.related_delivery_iteration < 0
                or self.related_delivery_iteration >= self.iteration
            ):
                raise ValueError(
                    "post-delivery receipt requires a non-negative delivery "
                    "iteration strictly before the control iteration"
                )
        elif self.related_delivery_iteration is not None:
            raise ValueError(
                "pre-delivery controls cannot declare a related delivery iteration"
            )
        if type(self.candidate_chars) is not int or self.candidate_chars < 0:
            raise ValueError("candidate_chars must be a non-negative integer")
        if bool(self.candidate_chars) != bool(self.candidate_sha256_16):
            raise ValueError("candidate byte identity requires chars and sha together")
        if self.candidate_sha256_16 and (
            len(self.candidate_sha256_16) != 16
            or any(c not in "0123456789abcdef" for c in self.candidate_sha256_16)
        ):
            raise ValueError("candidate_sha256_16 must be 16 lowercase hex chars")
        if contract.fact_class_required:
            if (
                not isinstance(self.fact_class, str)
                or not self.fact_class
                or self.fact_class == "*"
                or not is_registered(self.fact_class)
            ):
                raise ValueError(
                    "mediator fact_class must be one concrete registered FACT identity"
                )
        elif self.fact_class is not None and (
            not isinstance(self.fact_class, str) or not is_registered(self.fact_class)
        ):
            raise ValueError("fact_class must be None or a concrete registered FACT identity")
        if not isinstance(self.candidate_id, str):
            raise ValueError("candidate_id must be a string")
        if contract.fact_class_required and not self.candidate_id:
            raise ValueError("mediator candidate_id must be a non-empty canonical ID")
        if self.observation_binding is not None:
            issues = validate_observation_binding(
                self.observation_binding,
                expected_candidate_id=self.candidate_id or None,
            )
            if issues:
                raise ValueError("observation_binding invalid: " + ",".join(issues))


def build_control_participation(
    *,
    feature_id: str,
    decision_site: str,
    decision: str,
    iteration: int,
    candidate_bytes: str = "",
    fact_class: str | None = None,
    candidate_id: str = "",
    reason: str = "",
    related_delivery_iteration: int | None = None,
    observation_binding: ObservationBinding | None = None,
) -> ControlParticipation:
    contract = control_contract(feature_id)
    if contract.measurement_status != "SUPPORTED":
        raise ValueError(
            f"{feature_id} is UNMEASURED: {contract.unmeasured_reason}"
        )
    if decision_site not in contract.decision_sites:
        raise ValueError(
            f"unsupported decision site for {feature_id}: {decision_site!r}"
        )
    if not isinstance(candidate_bytes, str):
        raise TypeError("candidate_bytes must be text")
    seal = (
        hashlib.sha256(candidate_bytes.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        if candidate_bytes else ""
    )
    return ControlParticipation(
        schema=CONTROL_PARTICIPATION_SCHEMA,
        feature_id=feature_id,
        role=contract.role,
        decision_site=decision_site,
        decision=decision,
        iteration=iteration,
        candidate_chars=len(candidate_bytes),
        candidate_sha256_16=seal,
        fact_class=fact_class,
        reason=reason,
        candidate_id=candidate_id,
        temporal_relation=contract.temporal_relation,
        related_delivery_iteration=related_delivery_iteration,
        observation_binding=observation_binding,
    )


def participation_to_dict(record: ControlParticipation) -> dict[str, Any]:
    payload = {
        "schema": record.schema,
        "control_ref": {
            "category": "CAP",
            "feature_id": record.feature_id,
            "role": record.role,
        },
        "decision_site": record.decision_site,
        "decision": record.decision,
        "iteration": record.iteration,
        "candidate_chars": record.candidate_chars,
        "candidate_sha256_16": record.candidate_sha256_16,
        "fact_class": record.fact_class,
        "candidate_id": record.candidate_id,
        "temporal_relation": record.temporal_relation,
        "related_delivery_iteration": record.related_delivery_iteration,
        "reason": record.reason,
    }
    if record.observation_binding is not None:
        payload["observation_binding"] = observation_binding_to_dict(
            record.observation_binding
        )
    return payload


__all__ = [
    "CONTROL_DECISION_CONTRACTS",
    "CONTROL_PARTICIPATION_SCHEMA",
    "CONTROL_PRECEDES_DELIVERY",
    "PARTICIPATION_TEMPORAL_RELATIONS",
    "RECEIPT_FOLLOWS_DELIVERY",
    "ControlDecisionContract",
    "ControlParticipation",
    "build_control_participation",
    "control_contract",
    "participation_to_dict",
]
