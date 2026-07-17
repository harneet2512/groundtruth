"""Typed, render-neutral feature lineage for model-facing GT evidence.

The lineage is audit metadata.  It never participates in rendering, evidence
identity, deduplication, arbitration, or receipt promotion.  FACT identity is
resolved only through :mod:`groundtruth.runtime.fact_registry`; CAP ownership is
accepted only as an explicit producer claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .fact_registry import is_reactive, producer_matches, registration_for, required_event


LINEAGE_SCHEMA = "gt.feature_lineage.v1"
FEATURE_CATEGORIES = frozenset({"ACQ", "CAP", "FACT", "PERF"})
FEATURE_ROLES = frozenset({"fact", "byte_owner", "mediator", "eligibility"})
# PRODUCT DECISION 2 (P4, B-TERM 2026-07-16) — GT_SS_COHERENCE_V2 is NO LONGER a byte owner.
# It has no canonical FACT identity by design (its exact_profile_member stamp carried
# ``fact_class=None``), so the 7-gate byte-owner bar was structurally UNSATISFIABLE for it (its
# host-side measurement row is chars=0 → never a ``delivered`` row → ``delivered_byte_proven``
# could never be True). Reclassified here as a CONTROL/mediator (it falls into the residual
# CAP_MEDIATOR_IDS below): its SS-LIVE obligation becomes ``live_control_mediation_effect`` (the
# control terminal), which IS satisfiable. Its exact ``detect.coherence`` byte stamp REMAINS as
# byte evidence — but as a LANE profile-member stamp (``gt_mini_patch._LANE_PROFILE_MEMBER_OWNERS``),
# not a byte-owner-mechanism stamp. The 128-row inventory count is unchanged (only the role moves
# within CAP: byte_owner 8→7, mediator 26→27, eligibility 13). This is a revisitable product
# decision, documented in code so it can be re-litigated.
CAP_BYTE_OWNER_IDS = frozenset({
    "GT_EDIT_CHECK",
    "GT_CHANGE_SURFACE",
    "GT_PATCH_DELTA",
    "GT_HYPOTHESIS",
    "GT_LOC_RESLOT",
    "GT_SS_SUBMIT_RED",
    "GT_CERT_DELIVERY",
})
CAP_ELIGIBILITY_IDS = frozenset({
    "GT_OBLIGATION_FRESHNESS",
    "GT_SS_NOVELTY",
    "GT_SS_EXEC_TRUTH",
    "GT_EDIT_OVERLAY",
    "GT_REGISTRY_ENFORCE",
    "GT_SS_RECOVERY_V2",
    "GT_BRIEF_MINIMAL",
    "GT_SS_DEDUP2",
    "GT_SS_ELIGIBILITY",
    "GT_XSESSION_MEMORY",
    "GT_D7_RELATEDNESS",
    "GT_SS_SHADOW",
    "GT_SS_LATE_DROP",
})
CAP_FEATURE_IDS = frozenset({
    "GT_CONTRACT_NATIVE", "GT_SS_ACK_METRICS", "GT_OBLIGATION_FRESHNESS",
    "GT_L6_FRESH", "GT_SS_NOVELTY", "GT_SS_EXEC_TRUTH",
    "GT_GATEWAY_EDIT_BRIDGES", "GT_LANE_ENVELOPE", "GT_EDIT_OVERLAY",
    "GT_SS_RECOVERY_V2", "GT_BRIEF_MINIMAL", "GT_EVIDENCE_NATIVE",
    "GT_CHANGE_SURFACE", "GT_REGISTRY_ENFORCE", "GT_CONTRACT_BILATERAL",
    "GT_GATEWAY", "GT_STEER_NATIVE", "GT_NUDGE_NATIVE",
    "GT_POST_SEARCH_NATIVE", "GT_SS_COHERENCE_V2", "GT_SS_DEDUP2",
    "GT_CERT_DELIVERY", "GT_SS_SUBMIT_RED", "GT_LOC_RESLOT",
    "GT_SS_ARBITER_V2", "GT_COMPLETION_CERT", "GT_VERIFICATION_PLAN",
    "GT_SEM_BODY", "GT_SS_ELIGIBILITY", "GT_XSESSION_MEMORY",
    "GT_GLOBAL_ARBITER", "GT_SCOPE_NATIVE", "GT_EDIT_CHECK",
    "GT_D7_RELATEDNESS", "GT_GATEWAY_NATIVE", "GT_HYPOTHESIS",
    "GT_SS_ACK_FORM", "GT_SS_PROVENANCE", "GT_XSESSION_RANKUP",
    "GT_CONTENT_LEG", "GT_INSEAM_METRICS", "GT_SS_SHADOW",
    "GT_BRIEF_NATIVE", "GT_VERIFY_EXECUTE", "GT_CONTRACT_MODE",
    "GT_PATCH_DELTA", "GT_SS_LATE_DROP",
})
if CAP_BYTE_OWNER_IDS & CAP_ELIGIBILITY_IDS:
    raise ValueError("CAP role sets overlap")
if not (CAP_BYTE_OWNER_IDS | CAP_ELIGIBILITY_IDS) <= CAP_FEATURE_IDS:
    raise ValueError("CAP role set contains an unknown feature")
CAP_MEDIATOR_IDS = CAP_FEATURE_IDS - CAP_BYTE_OWNER_IDS - CAP_ELIGIBILITY_IDS


@dataclass(frozen=True, order=True)
class CAPByteOwnerBinding:
    """One exact producer/layer/FACT binding for a CAP byte owner."""

    producer: str
    layer: str
    fact_class: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.producer, str) or not self.producer.strip():
            raise ValueError("CAP byte-owner producer must be a non-empty string")
        if not isinstance(self.layer, str) or not self.layer.strip():
            raise ValueError("CAP byte-owner layer must be a non-empty string")
        if self.fact_class is not None and (
            not isinstance(self.fact_class, str) or not self.fact_class.strip()
        ):
            raise ValueError("CAP byte-owner fact_class must be None or non-empty")


@dataclass(frozen=True)
class CAPByteOwnerMechanism:
    """The sole permitted attribution mechanism for one byte-owning CAP."""

    mechanism: str
    bindings: tuple[CAPByteOwnerBinding, ...]

    def __post_init__(self) -> None:
        if self.mechanism not in {"typed_lineage", "exact_profile_member"}:
            raise ValueError(f"unknown CAP byte-owner mechanism: {self.mechanism!r}")
        if not self.bindings or self.bindings != tuple(sorted(set(self.bindings))):
            raise ValueError("CAP byte-owner bindings must be non-empty, unique, and sorted")
        if self.mechanism == "typed_lineage" and any(
            binding.fact_class is None for binding in self.bindings
        ):
            raise ValueError("typed lineage requires a registered FACT class")


def _bindings(*rows: tuple[str, str, str | None]) -> tuple[CAPByteOwnerBinding, ...]:
    return tuple(sorted(CAPByteOwnerBinding(*row) for row in rows))


# One authority for all seven byte owners. Exact-profile-member rows claim a FACT
# only when the byte-producing mechanism has a reviewed canonical identity.
# P4 (B-TERM 2026-07-16): GT_SS_COHERENCE_V2 was removed from this table when it was
# reclassified byte_owner → control/mediator (see CAP_BYTE_OWNER_IDS above). It had no
# canonical FACT identity (fact_class=None), so it could never satisfy the byte-owner bar;
# its ``detect.coherence`` byte stamp now lives ONLY as a lane profile-member stamp
# (``gt_mini_patch._LANE_PROFILE_MEMBER_OWNERS``), never fabricating a FACT.
CAP_BYTE_OWNER_MECHANISMS: Mapping[str, CAPByteOwnerMechanism] = MappingProxyType({
    "GT_CHANGE_SURFACE": CAPByteOwnerMechanism("typed_lineage", _bindings(
        ("change_surface", "missing_role", "newfile_precedent"),
        ("change_surface", "new_file_destination", "newfile_precedent"),
    )),
    "GT_PATCH_DELTA": CAPByteOwnerMechanism("typed_lineage", _bindings(
        ("patch_delta", "companion_surface", "signature_delta"),
        ("patch_delta", "signature_mismatch", "signature_delta"),
    )),
    "GT_LOC_RESLOT": CAPByteOwnerMechanism("typed_lineage", _bindings(
        ("ranked_localization", "localization", "localization"),
    )),
    "GT_SS_SUBMIT_RED": CAPByteOwnerMechanism("typed_lineage", _bindings(
        ("submit_gate", "submit_refusal", "submit_refusal"),
    )),
    "GT_EDIT_CHECK": CAPByteOwnerMechanism("exact_profile_member", _bindings(
        ("edit_check", "edit.syntax", "syntax_result"),
    )),
    "GT_HYPOTHESIS": CAPByteOwnerMechanism("exact_profile_member", _bindings(
        ("governor", "recovery", "recovery"),
    )),
    "GT_CERT_DELIVERY": CAPByteOwnerMechanism("exact_profile_member", _bindings(
        ("submit_gate", "submit_refusal", "submit_refusal"),
    )),
})
if set(CAP_BYTE_OWNER_MECHANISMS) != set(CAP_BYTE_OWNER_IDS):
    raise ValueError("CAP byte-owner mechanism table must cover exactly all byte owners")

# Compatibility view used by ``build_lineage``.  It is derived, never separately
# authored, so typed authorization cannot drift from the eight-row authority.
_TYPED_CAP_OWNER_BINDINGS: dict[str, frozenset[tuple[str, str, str]]] = {
    feature_id: frozenset(
        (binding.producer, binding.layer, binding.fact_class)
        for binding in authority.bindings
        if binding.fact_class is not None
    )
    for feature_id, authority in CAP_BYTE_OWNER_MECHANISMS.items()
    if authority.mechanism == "typed_lineage"
}


def cap_role_for(feature_id: str) -> str:
    """Return the explicit role of one authoritative CAP inventory row."""

    if feature_id not in CAP_FEATURE_IDS:
        raise ValueError(f"unknown CAP feature: {feature_id!r}")
    if feature_id in CAP_BYTE_OWNER_IDS:
        return "byte_owner"
    if feature_id in CAP_ELIGIBILITY_IDS:
        return "eligibility"
    if feature_id in CAP_MEDIATOR_IDS:
        return "mediator"
    raise AssertionError(f"unclassified CAP feature: {feature_id}")


@dataclass(frozen=True, order=True)
class FeatureRef:
    """One exact feature-inventory identity."""

    category: str
    feature_id: str
    role: str

    def __post_init__(self) -> None:
        if self.category not in FEATURE_CATEGORIES:
            raise ValueError(f"unknown feature category: {self.category!r}")
        if not isinstance(self.feature_id, str) or not self.feature_id.strip():
            raise ValueError("feature_id must be a non-empty string")
        if self.category == "CAP" and not self.feature_id.startswith("GT_"):
            raise ValueError("CAP feature_id must be an exact GT_* inventory id")
        if self.role not in FEATURE_ROLES:
            raise ValueError(f"unknown feature role: {self.role!r}")
        if self.category == "FACT" and self.role != "fact":
            raise ValueError("FACT features must use the fact role")
        if self.category == "CAP" and self.role != cap_role_for(self.feature_id):
            raise ValueError(
                f"CAP role mismatch for {self.feature_id}: {self.role} != "
                f"{cap_role_for(self.feature_id)}"
            )


@dataclass(frozen=True)
class DeliveryLineage:
    """Immutable producer-to-receipt lineage for one evidence candidate."""

    schema: str
    runtime_producer_id: str
    registered_producer_id: str
    # ``runtime_producer_id`` is an untrusted caller claim.  Only an exact
    # registry match can establish producer ownership for downstream joins.
    producer_registration_match: bool
    evidence_type: str
    fact_class: str
    features: tuple[FeatureRef, ...]
    required_event: str
    actual_event: str
    receipt_predicate: str
    causal_eval: str
    causal_probe_id: str
    causal_contribution_proven: bool
    reactive: bool

    def __post_init__(self) -> None:
        if self.schema != LINEAGE_SCHEMA:
            raise ValueError(f"unsupported lineage schema: {self.schema!r}")
        for name in (
            "runtime_producer_id",
            "registered_producer_id",
            "evidence_type",
            "fact_class",
            "required_event",
            "actual_event",
            "receipt_predicate",
            "causal_eval",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.features != tuple(sorted(set(self.features))):
            raise ValueError("features must be unique and deterministically sorted")
        fact_refs = tuple(ref for ref in self.features if ref.category == "FACT")
        if fact_refs != (FeatureRef("FACT", self.fact_class, "fact"),):
            raise ValueError("lineage must contain exactly its canonical FACT feature")
        if not isinstance(self.causal_probe_id, str):
            raise ValueError("causal_probe_id must be a string")
        if not isinstance(self.producer_registration_match, bool):
            raise ValueError("producer_registration_match must be a bool")
        if not isinstance(self.causal_contribution_proven, bool):
            raise ValueError("causal_contribution_proven must be a bool")
        if not isinstance(self.reactive, bool):
            raise ValueError("reactive must be a bool")


def _feature_refs(
    fact_class: str,
    cap_feature_ids: Iterable[str],
    *,
    runtime_producer_id: str,
    evidence_type: str,
) -> tuple[FeatureRef, ...]:
    refs = {FeatureRef("FACT", fact_class, "fact")}
    evidence_base = evidence_type.split(":", 1)[0]
    owners = tuple(dict.fromkeys(cap_feature_ids))
    if len(owners) > 1:
        raise ValueError("lineage may claim at most one CAP byte owner")
    for feature_id in owners:
        if feature_id not in CAP_BYTE_OWNER_IDS:
            raise ValueError(f"CAP is not a declared byte owner: {feature_id}")
        binding = (runtime_producer_id, evidence_base, fact_class)
        if binding not in _TYPED_CAP_OWNER_BINDINGS.get(feature_id, frozenset()):
            raise ValueError(
                f"CAP byte owner {feature_id} is not authorized for "
                f"producer/evidence {runtime_producer_id}/{evidence_type}"
            )
        refs.add(FeatureRef("CAP", feature_id, "byte_owner"))
    return tuple(sorted(refs))


def build_lineage(
    *,
    runtime_producer_id: str,
    evidence_type: str,
    actual_event: str,
    cap_feature_ids: Iterable[str] = (),
) -> DeliveryLineage | None:
    """Build registered FACT lineage plus explicit CAP ownership.

    Unknown evidence types return ``None``.  No feature is inferred from enabled
    flags, payload text, a layer name, or the existence of an artifact.
    """

    registration = registration_for(evidence_type)
    boundary = required_event(evidence_type)
    if registration is None or boundary is None:
        return None
    return DeliveryLineage(
        schema=LINEAGE_SCHEMA,
        runtime_producer_id=runtime_producer_id,
        registered_producer_id=registration.producer,
        producer_registration_match=producer_matches(evidence_type, runtime_producer_id),
        evidence_type=evidence_type,
        fact_class=registration.fact_class,
        features=_feature_refs(
            registration.fact_class,
            cap_feature_ids,
            runtime_producer_id=runtime_producer_id,
            evidence_type=evidence_type,
        ),
        required_event=boundary,
        actual_event=actual_event,
        receipt_predicate=registration.receipt_predicate,
        causal_eval=registration.causal_eval,
        causal_probe_id="",
        causal_contribution_proven=False,
        reactive=is_reactive(evidence_type),
    )


def lineage_to_dict(lineage: DeliveryLineage) -> dict:
    """Return a deterministic, JSON-native representation."""

    return {
        "schema": lineage.schema,
        "runtime_producer_id": lineage.runtime_producer_id,
        "registered_producer_id": lineage.registered_producer_id,
        "producer_registration_match": lineage.producer_registration_match,
        "evidence_type": lineage.evidence_type,
        "fact_class": lineage.fact_class,
        "features": [
            {"category": ref.category, "feature_id": ref.feature_id, "role": ref.role}
            for ref in lineage.features
        ],
        "required_event": lineage.required_event,
        "actual_event": lineage.actual_event,
        "receipt_predicate": lineage.receipt_predicate,
        "causal_eval": lineage.causal_eval,
        "causal_probe_id": lineage.causal_probe_id,
        "causal_contribution_proven": lineage.causal_contribution_proven,
        "reactive": lineage.reactive,
    }


def lineage_ledger_extra(lineage: DeliveryLineage | None) -> dict:
    """Flatten lineage into additive runtime-ledger columns."""

    if lineage is None:
        return {}
    payload = lineage_to_dict(lineage)
    return {
        "lineage_schema": payload["schema"],
        "runtime_producer_id": payload["runtime_producer_id"],
        "registered_producer_id": payload["registered_producer_id"],
        "producer_registration_match": payload["producer_registration_match"],
        "evidence_type": payload["evidence_type"],
        "fact_class": payload["fact_class"],
        "feature_ids": payload["features"],
        "required_event": payload["required_event"],
        "actual_event": payload["actual_event"],
        "receipt_predicate": payload["receipt_predicate"],
        "causal_eval": payload["causal_eval"],
        "causal_probe_id": payload["causal_probe_id"],
        "causal_contribution_proven": payload["causal_contribution_proven"],
        "reactive": payload["reactive"],
    }


__all__ = [
    "CAPByteOwnerBinding",
    "CAPByteOwnerMechanism",
    "CAP_BYTE_OWNER_MECHANISMS",
    "DeliveryLineage",
    "CAP_BYTE_OWNER_IDS",
    "CAP_ELIGIBILITY_IDS",
    "CAP_FEATURE_IDS",
    "CAP_MEDIATOR_IDS",
    "FEATURE_CATEGORIES",
    "FEATURE_ROLES",
    "FeatureRef",
    "LINEAGE_SCHEMA",
    "build_lineage",
    "cap_role_for",
    "lineage_ledger_extra",
    "lineage_to_dict",
]
