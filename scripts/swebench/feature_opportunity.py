#!/usr/bin/env python3
"""Fail-closed joins for host-only ``feature.opportunity`` ledger rows.

Opportunity rows prove only that a formatter-visible candidate existed at one
policy observation.  They do not prove delivery, timing, acknowledgment, or
causal effect and therefore never mutate an SS readiness object.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from groundtruth.runtime.evidence_envelope import (
    feature_opportunity_id as _shared_feature_opportunity_id,
    observation_binding_from_dict,
    policy_observation_id as _shared_policy_observation_id,
    validate_observation_binding,
)
from groundtruth.runtime.feature_lineage import FeatureRef
from consumption_ledger import (
    PHYSICAL_DELIVERY_BOUND,
    physical_delivery_authority,
)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REF_KEYS = frozenset({"category", "feature_id", "role"})


def policy_observation_id(
    start_iteration: int, parent_sha256: str, action_sha256: str,
) -> str:
    """Rebuild the seam's framed policy-observation identity."""
    return _shared_policy_observation_id(
        start_iteration, parent_sha256, action_sha256,
    )


def feature_opportunity_id(
    observation_id: str,
    ordinal: int,
    kind_sha256: str,
    dedup_sha256: str,
) -> str:
    """Rebuild the seam's framed candidate-opportunity identity."""
    return _shared_feature_opportunity_id(
        observation_id, ordinal, kind_sha256, dedup_sha256,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _action_batch_sha256(actions: list[Any]) -> str:
    keys = tuple(
        json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
        for action in actions
    )
    raw = json.dumps(keys, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(raw)


def _policy_observations(
    messages: list[Any],
) -> dict[str, list[tuple[int, int]]]:
    """Index exact parent policies using the seam's cumulative action clock."""
    result: dict[str, list[tuple[int, int]]] = defaultdict(list)
    action_count = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        extra = message.get("extra")
        actions = extra.get("actions", []) if isinstance(extra, dict) else []
        if not isinstance(actions, list):
            continue
        parent_sha = _sha256_text(content)
        action_sha = _action_batch_sha256(actions)
        observation_id = policy_observation_id(action_count, parent_sha, action_sha)
        result[observation_id].append((index, len(content)))
        action_count += len(actions)
    return dict(result)


def _parent_content_index(
    messages: list[Any],
) -> dict[str, list[tuple[int, int]]]:
    """Index parent-policy *content* by its sha256 — a clock-independent anchor.

    The observation-id join in :func:`_policy_observations` re-derives the seam's
    cumulative-action clock from the trajectory.  That clock is only a proxy: the
    seam's real ``_action_count`` is skipped on some turns (e.g. the precommitted
    handshake early-return) and saved/restored around winner-context replay, so a
    well-formed row's ``observation_id`` can legitimately fail to reconstruct
    offline even though its parent policy genuinely occurred.  Parent *content*
    identity does not depend on that clock and can prove the anchor independently.
    The message population mirrors :func:`_policy_observations` exactly."""
    result: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        extra = message.get("extra")
        actions = extra.get("actions", []) if isinstance(extra, dict) else []
        if not isinstance(actions, list):
            continue
        result[_sha256_text(content)].append((index, len(content)))
    return dict(result)


def _identifiable_refs(
    raw_refs: object, inventory: dict[str, tuple[str, ...]],
) -> list[str]:
    """Recover only canonical IDs for assigning a malformed-row blocker."""
    if not isinstance(raw_refs, list):
        return []
    found: list[str] = []
    for raw in raw_refs:
        if not isinstance(raw, dict):
            continue
        category, feature_id = raw.get("category"), raw.get("feature_id")
        if category in {"CAP", "FACT"} and feature_id in inventory[category]:
            found.append(str(feature_id))
    return sorted(set(found))


def _validated_refs(
    raw_refs: object, inventory: dict[str, tuple[str, ...]],
) -> tuple[list[dict[str, str]], list[str]]:
    issues: list[str] = []
    if not isinstance(raw_refs, list) or not raw_refs:
        return [], ["feature_refs"]
    typed: list[FeatureRef] = []
    for raw in raw_refs:
        if not isinstance(raw, dict) or set(raw) != _REF_KEYS:
            issues.append("feature_ref_shape")
            continue
        try:
            ref = FeatureRef(raw["category"], raw["feature_id"], raw["role"])
        except (KeyError, TypeError, ValueError):
            issues.append("feature_ref_value")
            continue
        if ref.category not in {"CAP", "FACT"}:
            issues.append("feature_ref_category")
            continue
        if ref.feature_id not in inventory[ref.category]:
            issues.append("feature_ref_inventory")
            continue
        typed.append(ref)
    if len(typed) != len(raw_refs):
        return [], sorted(set(issues))
    if typed != sorted(set(typed)):
        issues.append("feature_ref_order_or_duplicate")
    fact_refs = [ref for ref in typed if ref.category == "FACT"]
    cap_refs = [ref for ref in typed if ref.category == "CAP"]
    if len(fact_refs) != 1 or len(cap_refs) > 1:
        issues.append("feature_ref_cardinality")
    if issues:
        return [], sorted(set(issues))
    return [
        {"category": ref.category, "feature_id": ref.feature_id, "role": ref.role}
        for ref in typed
    ], []


def _validate_bound_row(
    row: dict[str, Any],
    inventory: dict[str, tuple[str, ...]],
    parent_index: dict[str, list[tuple[int, int]]],
    content_index: dict[str, list[tuple[int, int]]],
) -> tuple[list[dict[str, str]], list[str], int | None]:
    issues: list[str] = []
    if row.get("event_type") != "policy_observation":
        issues.append("event_type")
    if row.get("outcome") != "eligible" or row.get("reason") != "formatter_visible_candidate":
        issues.append("outcome")
    if row.get("chars_delivered") != 0:
        issues.append("chars_delivered")
    if row.get("attribution_status") != "BOUND" or row.get("attribution_reason") != "typed_lineage":
        issues.append("producer_binding")

    for field in (
        "observation_id", "opportunity_id", "parent_policy_sha256",
        "action_batch_sha256", "candidate_kind_sha256", "candidate_dedup_sha256",
    ):
        if not isinstance(row.get(field), str) or _HEX64.fullmatch(row[field]) is None:
            issues.append(field)
    for field in ("iteration", "parent_policy_chars", "candidate_ordinal"):
        value = row.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            issues.append(field)
    for field in ("delivery_eligible", "selected"):
        if not isinstance(row.get(field), bool):
            issues.append(field)
    candidate_id = row.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        issues.append("candidate_id")

    refs, ref_issues = _validated_refs(row.get("feature_refs"), inventory)
    issues.extend(ref_issues)
    if not issues:
        expected_observation = policy_observation_id(
            row["iteration"], row["parent_policy_sha256"], row["action_batch_sha256"]
        )
        if row["observation_id"] != expected_observation:
            issues.append("observation_id_mismatch")
        expected_opportunity = feature_opportunity_id(
            row["observation_id"], row["candidate_ordinal"],
            row["candidate_kind_sha256"], row["candidate_dedup_sha256"],
        )
        if row["opportunity_id"] != expected_opportunity:
            issues.append("opportunity_id_mismatch")
        expected_dedup_sha = _sha256_text(row["candidate_id"])
        if row["candidate_dedup_sha256"] not in {
            expected_dedup_sha,
            row["candidate_id"],
        }:
            issues.append("candidate_id_mismatch")
        binding_raw = row.get("observation_binding")
        if binding_raw is not None:
            try:
                binding = observation_binding_from_dict(binding_raw)
            except (TypeError, ValueError):
                issues.append("observation_binding")
            else:
                assert binding is not None
                issues.extend(validate_observation_binding(
                    binding, expected_candidate_id=row["candidate_id"],
                ))
                if binding.observation_id != row["observation_id"]:
                    issues.append("observation_binding_observation_mismatch")
                if binding.opportunity_id != row["opportunity_id"]:
                    issues.append("observation_binding_opportunity_mismatch")
    # Parent-policy anchor. The observation-id (action-clock) join is the *precise*
    # anchor when it reconstructs offline, but the seam's ``_action_count`` clock is
    # not always reproducible from the trajectory (turn-skips + replay save/restore),
    # so a well-formed row can miss it. A content-addressed match — the row's parent
    # policy content genuinely appearing as a trajectory assistant message at the
    # producer-recorded width — proves the anchor independently and is fail-closed:
    #   * parent content ABSENT      -> "parent_policy_join"          (malformed)
    #   * content present, width off -> "parent_policy_chars_mismatch" (malformed)
    #   * content present + clock hit -> precise anchor
    #   * content present, no/many clock hits -> content anchor (unique) or UNMEASURED
    #     chronology (repeated content) — NEVER malformed for a legitimately-shaped row.
    clock_hits = parent_index.get(str(row.get("observation_id") or ""), [])
    content_hits = content_index.get(str(row.get("parent_policy_sha256") or ""), [])
    parent_message_index: int | None = None
    if not content_hits:
        issues.append("parent_policy_join")
    else:
        width_hits = [
            (msg_index, chars) for msg_index, chars in content_hits
            if chars == row.get("parent_policy_chars")
        ]
        if not width_hits:
            issues.append("parent_policy_chars_mismatch")
        elif len(clock_hits) == 1:
            parent_message_index = clock_hits[0][0]
            if clock_hits[0][1] != row.get("parent_policy_chars"):
                issues.append("parent_policy_chars_mismatch")
        elif len(width_hits) == 1:
            parent_message_index = width_hits[0][0]
    return refs, sorted(set(issues)), parent_message_index


def _unmeasured(reason: str) -> dict[str, Any]:
    return {
        "status": "UNMEASURED",
        "reason": reason,
        "eligible_opportunity": None,
        "opportunity_count": 0,
        "delivery_eligible_count": 0,
        "selected_count": 0,
        "decision_boundary_evidence": [],
    }


def collect_feature_opportunities(
    ledger_rows: list[dict[str, Any]],
    messages: list[Any],
    inventory: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Validate and project candidate opportunities onto exact CAP/FACT IDs.

    The parent-policy join exposes the chronological anchor needed by a later
    precommit audit.  ``decision_open`` deliberately remains unknown here.
    """
    feature_ids = tuple(inventory["CAP"]) + tuple(inventory["FACT"])
    records = {feature: _unmeasured("no_bound_opportunity") for feature in feature_ids}
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_by_feature: dict[str, set[str]] = defaultdict(set)
    parent_index = _policy_observations(messages)
    content_index = _parent_content_index(messages)
    seen_ids: set[str] = set()
    malformed_rows = 0
    unbound_rows = 0
    bound_rows = 0

    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict) or row.get("layer") != "feature.opportunity":
            continue
        identifiable = _identifiable_refs(row.get("feature_refs"), inventory)
        if row.get("attribution_status") != "BOUND" or row.get("attribution_reason") != "typed_lineage":
            unbound_rows += 1
            for feature in identifiable:
                invalid_by_feature[feature].add("producer_binding")
            continue
        refs, issues, parent_message_index = _validate_bound_row(
            row, inventory, parent_index, content_index
        )
        opportunity_id = row.get("opportunity_id")
        if isinstance(opportunity_id, str) and opportunity_id in seen_ids:
            issues.append("duplicate_opportunity_id")
        if isinstance(opportunity_id, str):
            seen_ids.add(opportunity_id)
        target_features = identifiable or [ref["feature_id"] for ref in refs]
        if issues:
            malformed_rows += 1
            for feature in target_features:
                invalid_by_feature[feature].update(issues)
            continue
        bound_rows += 1
        boundary = {
            "ledger_index": row_index,
            "observation_id": row["observation_id"],
            "opportunity_id": row["opportunity_id"],
            "candidate_id": row["candidate_id"],
            "iteration": row["iteration"],
            "parent_message_index": parent_message_index,
            "parent_policy_sha256": row["parent_policy_sha256"],
            "action_batch_sha256": row["action_batch_sha256"],
            "parent_policy_joined": True,
            "delivery_eligible": row["delivery_eligible"],
            "selected": row["selected"],
            "decision_open": None,
            "precommit_status": "UNMEASURED:chronological_audit_required",
        }
        for ref in refs:
            evidence[ref["feature_id"]].append(boundary)

    for feature in feature_ids:
        if invalid_by_feature.get(feature):
            records[feature] = _unmeasured(
                "invalid_bound_opportunity:" + ",".join(sorted(invalid_by_feature[feature]))
            )
            continue
        feature_evidence = evidence.get(feature, [])
        if not feature_evidence:
            continue
        records[feature] = {
            "status": "BOUND",
            "reason": "producer_matched_typed_lineage",
            "eligible_opportunity": True,
            "opportunity_count": len(feature_evidence),
            "delivery_eligible_count": sum(
                item["delivery_eligible"] is True for item in feature_evidence
            ),
            "selected_count": sum(item["selected"] is True for item in feature_evidence),
            "decision_boundary_evidence": feature_evidence,
        }
    return {
        "features": records,
        "integrity": {
            "schema": "gt.feature_opportunity.integrity.v1",
            "publishable": malformed_rows == 0,
            "bound_rows": bound_rows,
            "unbound_rows": unbound_rows,
            "malformed_rows": malformed_rows,
            "proof_scope": "eligibility_anchor_only_no_ss_live_promotion",
        },
    }


DELIVERED_FIRE = "DELIVERED_FIRE"
BOUND_NO_OP = "BOUND_NO_OP"
MISSED_FIRE = "MISSED_FIRE"
BROKEN_BINDING = "BROKEN_BINDING"
# 2026-07-28: the disposition set must be TOTAL. Before this state existed, an opportunity row
# that failed validation was `continue`d out of the join and appeared in NEITHER `per_opportunity`
# NOR the roll-up -- it simply vanished. A vanished row reads downstream exactly like a row that
# never existed, which is the "silent zero" failure this whole module exists to prevent: an
# analysis that says "N opportunities, all accounted for" while N is quietly short.
#
# `collect_feature_opportunities` already counted malformed rows and poisoned the affected feature
# to UNMEASURED; the JOIN did not, so the two disagreed about the same corpus. Now every ledger row
# claiming to be an opportunity leaves exactly one record, and the counts reconcile.
MALFORMED_OPPORTUNITY = "MALFORMED_OPPORTUNITY"

_BINDING_ROW_FIELDS = (
    "observation_id", "opportunity_id", "parent_policy_sha256",
    "parent_policy_chars", "action_batch_sha256", "candidate_ordinal",
    "candidate_kind_sha256", "candidate_dedup_sha256", "candidate_id",
)


def _binding_dict_from_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("observation_binding")
    if isinstance(raw, dict):
        return dict(raw)
    return {
        "schema": "gt.observation_binding.v1",
        "batch_start_iteration": row.get("iteration"),
        **{field: row.get(field) for field in _BINDING_ROW_FIELDS},
    }


def _exact_binding_match(
    left: dict[str, Any], right: dict[str, Any],
) -> bool:
    return all(left.get(field) == right.get(field) for field in (
        "schema", "batch_start_iteration", *_BINDING_ROW_FIELDS,
    ))


def join_opportunity_deliveries(
    ledger_rows: list[dict[str, Any]],
    messages: list[Any],
    inventory: dict[str, tuple[str, ...]],
    consumption_ledger: dict[str, Any],
) -> dict[str, Any]:
    """Join each physical fire to exactly one originating policy opportunity.

    The join is deliberately stricter than co-occurrence: opportunity identity,
    candidate identity, eligibility, and the reusable physical-span authority must all
    agree. A selected eligible opportunity with no exact fire is ``MISSED_FIRE``;
    suppressed/non-selected opportunities are ``BOUND_NO_OP``; malformed reuse is
    ``BROKEN_BINDING``. No state promotes SS readiness by itself.
    """
    parent_index = _policy_observations(messages)
    content_index = _parent_content_index(messages)
    opportunities_by_id: dict[str, dict[str, Any]] = {}
    opportunity_records: list[dict[str, Any]] = []

    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict) or row.get("layer") != "feature.opportunity":
            continue
        refs, issues, parent_message_index = _validate_bound_row(
            row, inventory, parent_index, content_index,
        )
        opportunity_id = row.get("opportunity_id")
        if not isinstance(opportunity_id, str) or opportunity_id in opportunities_by_id:
            issues.append("duplicate_opportunity_id")
        binding_dict = _binding_dict_from_opportunity(row)
        if issues:
            # ACCOUNTED FOR, NOT DROPPED. The row is unusable for the fire join -- its refs or
            # binding did not validate -- but it still consumed an opportunity, and the census
            # must say so. Emitting it with its issues named is the difference between "this
            # opportunity could not be evaluated, here is why" and silence, which downstream
            # cannot distinguish from "this opportunity never happened".
            record = {
                "ledger_row_index": row_index,
                "observation_id": row.get("observation_id"),
                "opportunity_id": opportunity_id if isinstance(opportunity_id, str) else None,
                "candidate_id": row.get("candidate_id"),
                "parent_message_index": parent_message_index,
                "delivery_eligible": row.get("delivery_eligible"),
                "selected": row.get("selected"),
                "feature_refs": refs,
                "state": MALFORMED_OPPORTUNITY,
                "reason": "invalid_bound_opportunity:" + ",".join(sorted(issues)),
                "physical_id": None,
                "binding": binding_dict,
            }
            opportunity_records.append(record)
            # Deliberately NOT indexed into `opportunities_by_id`: a malformed row must never
            # become the originating opportunity for a delivery. A fire that claims it stays
            # BROKEN_BINDING, which is the honest verdict.
            continue
        try:
            binding = observation_binding_from_dict(binding_dict)
        except (TypeError, ValueError) as exc:
            # The SECOND silent drop, and it was invisible even to the malformed-row counter in
            # `collect_feature_opportunities`, because that function never rehydrates the binding.
            # A row can pass field validation and still fail to construct a binding.
            opportunity_records.append({
                "ledger_row_index": row_index,
                "observation_id": row.get("observation_id"),
                "opportunity_id": opportunity_id if isinstance(opportunity_id, str) else None,
                "candidate_id": row.get("candidate_id"),
                "parent_message_index": parent_message_index,
                "delivery_eligible": row.get("delivery_eligible"),
                "selected": row.get("selected"),
                "feature_refs": refs,
                "state": MALFORMED_OPPORTUNITY,
                "reason": f"unconstructible_observation_binding:{type(exc).__name__}",
                "physical_id": None,
                "binding": binding_dict,
            })
            continue
        assert binding is not None
        assert isinstance(opportunity_id, str)
        state = (
            MISSED_FIRE
            if row.get("selected") is True and row.get("delivery_eligible") is True
            else BOUND_NO_OP
        )
        record = {
            "ledger_row_index": row_index,
            "observation_id": binding.observation_id,
            "opportunity_id": binding.opportunity_id,
            "candidate_id": binding.candidate_id,
            "parent_message_index": parent_message_index,
            "delivery_eligible": row["delivery_eligible"],
            "selected": row["selected"],
            "feature_refs": refs,
            "state": state,
            "reason": (
                "selected_eligible_without_exact_fire"
                if state == MISSED_FIRE else "bound_candidate_not_fired"
            ),
            "physical_id": None,
            "binding": binding_dict,
        }
        opportunities_by_id[opportunity_id] = record
        opportunity_records.append(record)

    physical = physical_delivery_authority(consumption_ledger).get("deliveries", {})
    per_delivery: list[dict[str, Any]] = []
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict):
            continue
        try:
            chars = int(row.get("chars_delivered") or 0)
        except (TypeError, ValueError):
            continue
        if row.get("outcome") != "delivered" or chars <= 0:
            continue
        raw_binding = row.get("observation_binding")
        if not isinstance(raw_binding, dict):
            per_delivery.append({
                "ledger_row_index": row_index,
                "state": BROKEN_BINDING,
                "reason": "delivery_missing_observation_binding",
                "observation_id": None,
                "opportunity_id": None,
                "physical_id": None,
            })
            continue
        opportunity_id = raw_binding.get("opportunity_id")
        origin = opportunities_by_id.get(str(opportunity_id or ""))
        if origin is None:
            per_delivery.append({
                "ledger_row_index": row_index,
                "state": BROKEN_BINDING,
                "reason": "fire_without_exact_originating_opportunity",
                "observation_id": raw_binding.get("observation_id"),
                "opportunity_id": opportunity_id,
                "physical_id": None,
            })
            continue
        if not _exact_binding_match(raw_binding, origin["binding"]):
            reason = "fire_without_exact_originating_opportunity"
        elif row.get("candidate_id") != origin["candidate_id"]:
            reason = "delivery_candidate_identity_mismatch"
        elif origin["delivery_eligible"] is not True:
            reason = "delivery_contradicts_ineligible_opportunity"
        else:
            reason = ""
        physical_record = physical.get(str(row_index), {})
        if not reason and physical_record.get("state") != PHYSICAL_DELIVERY_BOUND:
            reason = str(
                physical_record.get("reason") or "physical_delivery_unjoined"
            )
        physical_binding = physical_record.get("observation_binding")
        if (
            not reason and isinstance(physical_binding, dict)
            and not _exact_binding_match(raw_binding, physical_binding)
        ):
            reason = "cross_observation_physical_reuse"
        if reason:
            state = BROKEN_BINDING
            physical_id = physical_record.get("physical_id")
        else:
            state = DELIVERED_FIRE
            physical_id = physical_record.get("physical_id")
            origin["state"] = DELIVERED_FIRE
            origin["reason"] = "exact_opportunity_and_physical_span_join"
            origin["physical_id"] = physical_id
        per_delivery.append({
            "ledger_row_index": row_index,
            "state": state,
            "reason": reason or "exact_opportunity_and_physical_span_join",
            "observation_id": raw_binding.get("observation_id"),
            "opportunity_id": opportunity_id,
            "physical_id": physical_id,
        })

    state_rank = {
        BOUND_NO_OP: 0,
        DELIVERED_FIRE: 1,
        MISSED_FIRE: 2,
        BROKEN_BINDING: 3,
    }
    feature_states: dict[str, list[str]] = defaultdict(list)
    for record in opportunity_records:
        for ref in record["feature_refs"]:
            feature_states[ref["feature_id"]].append(record["state"])
    features = {
        feature_id: {
            "worst_state": max(states, key=state_rank.__getitem__),
            "states": states,
        }
        for feature_id, states in sorted(feature_states.items())
    }
    return {
        "schema": "gt.feature_opportunity_delivery_join.v1",
        "per_opportunity": opportunity_records,
        "per_delivery": per_delivery,
        "features": features,
    }


def attach_opportunity_evidence(
    feature_row: dict[str, Any], evidence: dict[str, Any],
) -> dict[str, Any]:
    """Attach an additive audit projection without touching terminal readiness."""
    result = dict(feature_row)
    result["opportunity_evidence"] = evidence
    return result


__all__ = [
    "BOUND_NO_OP",
    "BROKEN_BINDING",
    "DELIVERED_FIRE",
    "MISSED_FIRE",
    "attach_opportunity_evidence",
    "collect_feature_opportunities",
    "feature_opportunity_id",
    "join_opportunity_deliveries",
    "policy_observation_id",
]
