"""Mini-swe compatibility shim for the product-owned context policy."""
from __future__ import annotations

from groundtruth.runtime.context_policy import (  # noqa: F401
    EVENT_BOUND_PAYLOADS,
    PHASE_POLICY,
    POLICY_VERSION,
    Event,
    PayloadKind,
    Phase,
    allowed_payloads,
    phase_allows,
    should_emit,
)

__all__ = [
    "EVENT_BOUND_PAYLOADS",
    "PHASE_POLICY",
    "POLICY_VERSION",
    "Event",
    "PayloadKind",
    "Phase",
    "allowed_payloads",
    "phase_allows",
    "should_emit",
]
