"""Exact terminal candidate identity for post-delivery acknowledgment grading.

This module does not match assistant text, emit ledger rows, or claim causality.  It
only converts a later receipt decision into typed CAP participation bound to the
original final candidate bytes, registered FACT, and canonical candidate identity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .control_participation import ControlParticipation, build_control_participation
from .fact_registry import is_registered


ACK_FEATURE_ID = "GT_SS_ACK_METRICS"
ACK_DECISION_SITE = "mini_seam.acknowledgment.receipt_grading"


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class TerminalAckIdentity:
    """The exact delivered candidate watched for a later assistant receipt."""

    candidate_text: str
    fact_class: str
    candidate_id: str
    delivered_iteration: int

    @property
    def candidate_sha256_16(self) -> str:
        return hashlib.sha256(
            self.candidate_text.encode("utf-8", "surrogatepass")
        ).hexdigest()[:16]


def _validate_identity(identity: object) -> TerminalAckIdentity:
    if not isinstance(identity, TerminalAckIdentity):
        raise TypeError("identity must be TerminalAckIdentity")
    if not isinstance(identity.candidate_text, str) or not identity.candidate_text:
        raise ValueError("candidate_text must be non-empty text")
    if not isinstance(identity.fact_class, str) or not is_registered(identity.fact_class):
        raise ValueError("fact_class must be a concrete registered FACT")
    if not isinstance(identity.candidate_id, str) or not identity.candidate_id:
        raise ValueError("candidate_id must be non-empty text")
    if not _nonnegative_int(identity.delivered_iteration):
        raise ValueError("delivered_iteration must be a non-negative integer")
    return identity


def build_ack_participation(
    identity: TerminalAckIdentity,
    *,
    acknowledgment_iteration: int,
    acknowledged: bool,
) -> ControlParticipation:
    """Build a post-delivery receipt decision without promoting causal contribution."""

    exact = _validate_identity(identity)
    if (
        not _nonnegative_int(acknowledgment_iteration)
        or acknowledgment_iteration <= exact.delivered_iteration
    ):
        raise ValueError(
            "acknowledgment_iteration must be strictly later than delivered_iteration"
        )
    if not isinstance(acknowledged, bool):
        raise TypeError("acknowledged must be a bool")
    return build_control_participation(
        feature_id=ACK_FEATURE_ID,
        decision_site=ACK_DECISION_SITE,
        decision="APPLIED" if acknowledged else "NO_EFFECT",
        iteration=acknowledgment_iteration,
        candidate_bytes=exact.candidate_text,
        fact_class=exact.fact_class,
        candidate_id=exact.candidate_id,
        related_delivery_iteration=exact.delivered_iteration,
        reason=(
            "later_assistant_acknowledgment_observed"
            if acknowledged
            else "receipt_window_expired_without_acknowledgment"
        ),
    )


__all__ = [
    "ACK_DECISION_SITE",
    "ACK_FEATURE_ID",
    "TerminalAckIdentity",
    "build_ack_participation",
]
