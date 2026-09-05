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
# not a byte-owner-mechanism stamp. (That P4 role move was inventory-count-neutral: byte_owner
# 8→7, mediator 26→27, eligibility 13.) SEPARATELY, Cluster-5 ITEM 0 (2026-07-18) added the
# post_search master GT_POST_SEARCH as an ELIGIBILITY member — CAP 47→48, eligibility 13→14,
# total inventory 128→129. This is a revisitable product decision, documented in code.
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
    # ITEM 0 (2026-07-18): the post_search lattice MASTER enable is an ELIGIBILITY gate — it
    # decides WHETHER the post_search def-partition producer runs at all (gt_mini_patch
    # _POST_SEARCH_ON), the same lattice GT_SS_ELIGIBILITY widens. Never owns bytes.
    "GT_POST_SEARCH",
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
    "GT_POST_SEARCH",  # ITEM 0 (2026-07-18): post_search lattice master enable (eligibility)
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
        ("change_surface", "missing_role_postcreate", "newfile_precedent"),
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
    # TWO bindings, one per PLANE, both naming the SAME registered row.
    #   ("edit_check", "edit.syntax",    "syntax_result") — the LANE ledger layer. Consumed by
    #     the lane stamp path (``gt_mini_patch._exact_profile_delivery_extra`` matches
    #     ``b.layer == kind`` where ``kind`` is the ledger layer) and by the exact-profile
    #     branches of ``gt_feature_metrics`` (``binding.layer == row["layer"]``).
    #   ("edit_check", "syntax_result", "syntax_result") — the CANONICAL evidence type. Consumed
    #     by ``_feature_refs`` below, which matches ``binding.layer`` against
    #     ``evidence_type.split(":")[0]``.
    # WHY BOTH (2026-07-28): the ``layer`` field is overloaded BY MECHANISM — typed_lineage rows
    # spell an evidence_type there (``missing_role``/``signature_mismatch``/``localization``/
    # ``submit_refusal`` are all registered evidence types), exact_profile rows spell a lane
    # layer. ``registration_for("edit.syntax")`` is None, so the lane spelling alone could never
    # authorize a canonical CAP ref and GT_EDIT_CHECK was DARK on the canonical plane while LIVE
    # on the lane. Adding the canonical spelling INVENTS NOTHING: ``syntax_result`` is §1 FACT row
    # 5 of 11 and its registered producer is literally ``edit_check``
    # (fact_registry._REGISTRATIONS), so the registry itself already asserts this exact triple.
    # This is the l3.cochange test applied and PASSED — l3.cochange was kept out because no
    # cochange row existed to name; here the row exists and is named verbatim. The lane path
    # already performs this same layer→evidence-type resolution at runtime
    # (``gt_mini_patch:10693`` passes ``binding.fact_class`` as the evidence_type), so this is a
    # correction of the DECLARATION to match the behaviour, not a new claim.
    "GT_EDIT_CHECK": CAPByteOwnerMechanism("exact_profile_member", _bindings(
        ("edit_check", "edit.syntax", "syntax_result"),
        ("edit_check", "syntax_result", "syntax_result"),
    )),
    "GT_HYPOTHESIS": CAPByteOwnerMechanism("exact_profile_member", _bindings(
        ("governor", "recovery", "recovery"),
        ("governor", "verify.horizon.pivot", "recovery"),
    )),
    "GT_CERT_DELIVERY": CAPByteOwnerMechanism("exact_profile_member", _bindings(
        ("submit_gate", "submit_refusal", "submit_refusal"),
    )),
})
if set(CAP_BYTE_OWNER_MECHANISMS) != set(CAP_BYTE_OWNER_IDS):
    raise ValueError("CAP byte-owner mechanism table must cover exactly all byte owners")

def _binding_is_registry_valid(binding: CAPByteOwnerBinding) -> bool:
    """Whether the fact registry ITSELF already asserts this exact producer/type/FACT triple.

    Authorization is delegated to :mod:`fact_registry` — the same authority
    :func:`build_lineage` consults for the FACT identity — so no CAP can be granted an
    identity the registry does not independently attest:

      * ``registration_for(binding.layer)`` must resolve (the ``layer`` must be a REGISTERED
        evidence type, directly or through the alias/``base:suffix`` table), and
      * its canonical ``fact_class`` must equal the binding's declared ``fact_class``, and
      * ``producer_matches`` must accept the binding's producer for that evidence type.

    Anything else — notably a LANE ledger layer such as ``edit.syntax`` or
    ``verify.horizon.pivot``, which resolve to no registered class — is refused, and the
    refusal is recorded in :data:`CAP_BYTE_OWNER_BINDING_EXCLUSIONS` rather than being silent.
    """

    if binding.fact_class is None:
        return False
    registration = registration_for(binding.layer)
    if registration is None or registration.fact_class != binding.fact_class:
        return False
    return producer_matches(binding.layer, binding.producer)


# Authorization view used by ``build_lineage``.  It is DERIVED from the seven-row authority,
# never separately authored, so typed authorization cannot drift from it.
#
# WAS (until 2026-07-28): filtered on ``authority.mechanism == "typed_lineage"``.  That gate
# was a PROXY, and it was measuring the wrong thing.  It silently made every
# ``exact_profile_member`` owner — GT_CERT_DELIVERY, GT_EDIT_CHECK, GT_HYPOTHESIS, exactly
# three — un-authorizable on the CANONICAL plane while they stayed LIVE on the legacy lane,
# even though all seven carry a canonical contract requiring
# ``TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT``.  Two of those three were
# collateral damage: GT_CERT_DELIVERY's binding ``('submit_gate','submit_refusal',
# 'submit_refusal')`` is BYTE-IDENTICAL to GT_SS_SUBMIT_RED's, which the same table
# authorized; GT_HYPOTHESIS's ``('governor','recovery','recovery')`` is a verbatim §1 row.
# The mechanism label describes HOW the lane stamps a row, not WHETHER the registry can
# vouch for the identity — so the filter now asks the registry directly.
_TYPED_CAP_OWNER_BINDINGS: dict[str, frozenset[tuple[str, str, str]]] = {
    feature_id: frozenset(
        (binding.producer, binding.layer, binding.fact_class)
        for binding in authority.bindings
        if _binding_is_registry_valid(binding)
    )
    for feature_id, authority in CAP_BYTE_OWNER_MECHANISMS.items()
}

# NAMED, COUNTABLE exclusions — never a silent refusal.
#
# The mission rule cuts both ways: do not classify a silent feature as healthy, and do not
# classify a by-design exclusion as breakage.  A ``ValueError`` alone reads as breakage, so
# every binding the registry declines to vouch for is enumerated here WITH its reason.  A
# binding appearing here is NOT a defect: the two present entries are LANE ledger layers,
# which are the correct spelling for the lane stamp path and simply have no meaning on the
# canonical plane (their owners each carry a sibling canonical binding).
_EXCLUSION_UNREGISTERED_LAYER = "layer_is_not_a_registered_evidence_type"
_EXCLUSION_FACT_MISMATCH = "registered_fact_class_differs_from_binding"
_EXCLUSION_PRODUCER_UNAUTHORIZED = "producer_not_authoritative_for_evidence_type"
_EXCLUSION_NO_FACT = "binding_declares_no_fact_class"


def _exclusion_reason(binding: CAPByteOwnerBinding) -> str | None:
    if binding.fact_class is None:
        return _EXCLUSION_NO_FACT
    registration = registration_for(binding.layer)
    if registration is None:
        return _EXCLUSION_UNREGISTERED_LAYER
    if registration.fact_class != binding.fact_class:
        return _EXCLUSION_FACT_MISMATCH
    if not producer_matches(binding.layer, binding.producer):
        return _EXCLUSION_PRODUCER_UNAUTHORIZED
    return None


CAP_BYTE_OWNER_BINDING_EXCLUSIONS: tuple[tuple[str, CAPByteOwnerBinding, str], ...] = tuple(
    sorted(
        (feature_id, binding, reason)
        for feature_id, authority in CAP_BYTE_OWNER_MECHANISMS.items()
        for binding in authority.bindings
        for reason in (_exclusion_reason(binding),)
        if reason is not None
    )
)

# Owners with NO canonical byte-owning path at all.  This is the GT_SS_COHERENCE_V2 shape
# (chars=0 measurement row → never a ``delivered`` row → the byte-owner bar is structurally
# unsatisfiable), and such an owner belongs OUT of ``CAP_BYTE_OWNER_IDS`` entirely rather
# than inside it and permanently dark.  The set is empty today and the import-time check
# below fails LOUD if a future edit re-darkens an owner without recording it here.
#
# VERIFIED 2026-07-28 that GT_CERT_DELIVERY must NOT be given a ``completion_cert`` binding
# to close its gap: ``gt_mini_patch._gt_completion_cert_record`` records that layer with no
# ``chars`` argument (default 0), and ``_runtime_ledger_record`` downgrades any
# ``delivered`` outcome with ``chars<=0`` to ``suppressed_internal_only`` — so a
# ``completion_cert`` row can never be a delivered row.  Its real model-facing bytes ride
# ``submit_refusal`` (the ``_completion_cert_block`` rejection returned at the submit
# boundary), which is exactly the binding it already declares.
CAP_OWNERS_WITHOUT_CANONICAL_PATH: frozenset[str] = frozenset()


def canonical_binding_exclusions() -> tuple[dict, ...]:
    """Deterministic, JSON-native census of every declined byte-owner binding."""

    return tuple(
        {
            "feature_id": feature_id,
            "producer": binding.producer,
            "layer": binding.layer,
            "fact_class": binding.fact_class,
            "reason": reason,
            "owner_has_canonical_binding": bool(
                _TYPED_CAP_OWNER_BINDINGS.get(feature_id)
            ),
        }
        for feature_id, binding, reason in CAP_BYTE_OWNER_BINDING_EXCLUSIONS
    )


_owners_without_canonical_binding = frozenset(
    feature_id
    for feature_id in CAP_BYTE_OWNER_IDS
    if not _TYPED_CAP_OWNER_BINDINGS.get(feature_id)
)
if _owners_without_canonical_binding != CAP_OWNERS_WITHOUT_CANONICAL_PATH:
    raise ValueError(
        "CAP byte owners with no registry-valid canonical binding "
        f"{sorted(_owners_without_canonical_binding)} != the declared exemption set "
        f"{sorted(CAP_OWNERS_WITHOUT_CANONICAL_PATH)} — a byte owner that cannot own "
        "canonical bytes must be named here with a reason, or reclassified out of "
        "CAP_BYTE_OWNER_IDS (the GT_SS_COHERENCE_V2 precedent), never left silently dark"
    )


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
            declined = next(
                (
                    reason
                    for owner, declared, reason in CAP_BYTE_OWNER_BINDING_EXCLUSIONS
                    if owner == feature_id
                    and (declared.producer, declared.layer, declared.fact_class) == binding
                ),
                "",
            )
            raise ValueError(
                f"CAP byte owner {feature_id} is not authorized for "
                f"producer/evidence {runtime_producer_id}/{evidence_type}"
                + (
                    f" — declared but declined ({declined}); see "
                    "canonical_binding_exclusions()"
                    if declined
                    else " (no such declared binding)"
                )
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
    "CAP_BYTE_OWNER_BINDING_EXCLUSIONS",
    "CAP_OWNERS_WITHOUT_CANONICAL_PATH",
    "canonical_binding_exclusions",
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
