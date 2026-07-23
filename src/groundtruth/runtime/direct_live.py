"""DIRECT LIVE bar — sealed delivery only (never ON / brief-tag / evaluated).

Scorecard vocabulary for the 17 DIRECT features (10 FACT + 7 CAP byte-owners):

  DELIVERED      — durable ledger outcome=delivered, chars>0, content_sha256_16,
                   joinable fact_class / byte-owner lineage, bytes in observation
  HOLD           — opportunity existed; producer explicitly abstained with reason
  NOT_ELIGIBLE   — no bound opportunity on this trajectory
  BROKEN         — opportunity existed; silent, ERROR, or false-live claim

Anything less than DELIVERED is not LIVE. Profile-2 member ON, control_decision
APPLIED/evaluated, and brief orientation tags alone are never LIVE credit.
"""
from __future__ import annotations

from typing import Any, Mapping

from groundtruth.runtime.feature_lineage import CAP_BYTE_OWNER_IDS
from groundtruth.runtime.fact_registry import all_fact_classes

# Model-facing FACT classes that participate in the 17 DIRECT set.
DIRECT_FACT_CLASSES: frozenset[str] = frozenset({
    "obligations",
    "localization",
    "def_partition",
    "caller_contract",
    "syntax_result",
    "signature_delta",
    "covering_red",
    "submit_refusal",
    "newfile_precedent",
    "recovery",
})

DIRECT_CAP_BYTE_OWNERS: frozenset[str] = frozenset(CAP_BYTE_OWNER_IDS)

DIRECT_IDS: frozenset[str] = DIRECT_FACT_CLASSES | DIRECT_CAP_BYTE_OWNERS

LIVE_STATUS = frozenset({"DELIVERED", "HOLD", "NOT_ELIGIBLE", "BROKEN"})

# Claims that must NEVER be treated as LIVE for DIRECT rows.
FALSE_LIVE_CLAIM_KINDS: frozenset[str] = frozenset({
    "profile_member_on",
    "control_decision_evaluated",
    "control_decision_applied",
    "brief_tag_present",
    "opportunity_only",
    "produced_not_delivered",
    "chars_zero_delivered_label",
    "measurement_failed",
    "suppressed_without_hold_terminal",
})


def assert_direct_inventory_closed() -> None:
    """Fail closed if DIRECT FACT set drifts from the registry."""
    registered = set(all_fact_classes())
    missing = DIRECT_FACT_CLASSES - registered
    if missing:
        raise ValueError(f"DIRECT FACT classes missing from registry: {sorted(missing)}")


def is_sealed_delivery(row: Mapping[str, Any]) -> bool:
    """True iff a durable ledger row is a sealed model-visible delivery."""
    if not isinstance(row, Mapping):
        return False
    if str(row.get("outcome") or "") != "delivered":
        return False
    try:
        chars = int(row.get("chars_delivered") or 0)
    except (TypeError, ValueError):
        chars = 0
    if chars <= 0:
        return False
    seal = row.get("content_sha256_16")
    if not isinstance(seal, str) or len(seal) < 8:
        return False
    return True


def direct_id_from_row(row: Mapping[str, Any]) -> str | None:
    """Best-effort DIRECT id from a ledger / participation row."""
    if not isinstance(row, Mapping):
        return None
    fc = row.get("fact_class")
    if isinstance(fc, str) and fc in DIRECT_FACT_CLASSES:
        return fc
    pm = row.get("profile_member")
    if isinstance(pm, str) and pm in DIRECT_CAP_BYTE_OWNERS:
        return pm
    extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
    pm2 = extra.get("profile_member") if isinstance(extra, Mapping) else None
    if isinstance(pm2, str) and pm2 in DIRECT_CAP_BYTE_OWNERS:
        return pm2
    cref = row.get("control_ref")
    if isinstance(cref, Mapping):
        fid = cref.get("feature_id")
        if isinstance(fid, str) and fid in DIRECT_CAP_BYTE_OWNERS:
            return fid
    return None


def classify_direct_live(
    *,
    opportunity: bool,
    sealed_delivery: bool,
    explicit_hold: bool = False,
    measurement_error: bool = False,
    false_live_claim: str | None = None,
) -> str:
    """Return DELIVERED | HOLD | NOT_ELIGIBLE | BROKEN for one DIRECT row."""
    if sealed_delivery:
        return "DELIVERED"
    if not opportunity:
        return "NOT_ELIGIBLE"
    if explicit_hold:
        return "HOLD"
    if measurement_error:
        return "BROKEN"
    if false_live_claim and false_live_claim in FALSE_LIVE_CLAIM_KINDS:
        return "BROKEN"
    return "BROKEN"


__all__ = [
    "DIRECT_CAP_BYTE_OWNERS",
    "DIRECT_FACT_CLASSES",
    "DIRECT_IDS",
    "FALSE_LIVE_CLAIM_KINDS",
    "LIVE_STATUS",
    "assert_direct_inventory_closed",
    "classify_direct_live",
    "direct_id_from_row",
    "is_sealed_delivery",
]
