"""CP013 phase policy — shared product module (P0-11).

Phase detection still lives in gt_mini_patch runtime globals; this module is the
testable allowlist contract imported by the patch and unit tests.
"""
from __future__ import annotations

import enum
from typing import FrozenSet


class Phase(enum.Enum):
    ORIENT = "orient"
    SEARCH = "search"
    EDIT = "edit"
    VERIFY = "verify"
    SUBMIT = "submit"


PHASE_POLICY: dict[Phase, frozenset[str]] = {
    Phase.ORIENT: frozenset({"consensus.scope"}),
    Phase.SEARCH: frozenset({"l3b.evidence"}),
    Phase.EDIT: frozenset({
        "l3b.evidence", "spec.obligation", "l3.contract", "l3.cochange",
        "detect.coherence",
    }),
    Phase.VERIFY: frozenset({
        "spec.obligation", "l5.stuck", "l5.failure", "l5.no_test",
        "detect.loop", "verify.horizon.advisory", "verify.horizon.urgent",
        "verify.horizon.pivot",
    }),
    Phase.SUBMIT: frozenset({
        "spec.obligation", "verify.horizon.gate",
    }),
}


def phase_allows(kind: str, phase: Phase, policy: dict[Phase, FrozenSet[str]] | None = None) -> bool:
    """Return True when ``kind`` may fire in ``phase`` per the policy table."""
    allowed = (policy or PHASE_POLICY).get(phase, frozenset())
    if kind in allowed:
        return True
    if kind.startswith("verify.horizon."):
        return any(
            k.startswith("verify.horizon.") or k == "horizon.gate"
            for k in allowed
        )
    return False
