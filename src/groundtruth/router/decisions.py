"""RouterEmission + reason enums (FINAL_ARCH_V2 §3 Layer 3)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class EmissionKind(str, enum.Enum):
    """What the router decided to render (or would have rendered)."""

    NONE = "none"
    ON_VIEW_NEIGHBORHOOD = "on_view_neighborhood"
    ON_EDIT_CONTRACT = "on_edit_contract"
    ON_DRIFT_REDIRECT = "on_drift_redirect"
    ON_POST_EDIT_WARNING = "on_post_edit_warning"  # forwarded from Layer 5


class SuppressionReason(str, enum.Enum):
    """Why the router did NOT emit. Mirrors FINAL_ARCH_V2 §3 Layer 3 list."""

    DUPLICATE = "duplicate"           # evidence already shown for this target
    STALE = "stale"                   # would point at an already-viewed file
    TOO_LATE = "too_late"             # LATE/FINAL band + non-finalization signal
    NO_NEW_EDGE = "no_new_edge"       # provider returned only already-known edges
    BUDGET = "budget"                 # per-task injection cap reached
    LOW_CONFIDENCE = "low_confidence" # provider's best evidence below threshold
    NO_EVIDENCE = "no_evidence"       # provider returned nothing (graph present, but empty for this target)
    NO_GRAPH_DB = "no_graph_db"       # graph.db missing for this task; provider not consulted
    DEBOUNCE = "debounce"             # same-kind emission within debounce window
    NOT_APPLICABLE = "not_applicable" # router rule did not trigger
    DISABLED = "disabled"             # injection disabled (legacy state reset)


@dataclass
class RouterEmission:
    """One router decision. Always returned, even when suppressed.

    Use ``emit`` to ask "should the wrapper render this to the agent?". Use
    ``suppression_reason`` for telemetry / metric attribution.
    """

    kind: EmissionKind = EmissionKind.NONE
    emit: bool = False
    suppression_reason: SuppressionReason | None = SuppressionReason.NOT_APPLICABLE
    suppression_detail: str = ""
    evidence_text: str = ""
    primary_edge_file: str = ""
    next_action_type: str = ""
    next_action_file: str = ""
    evidence_items: list[dict[str, Any]] = field(default_factory=list)
    # For paired-replay analytics:
    target_file: str = ""
    target_functions: list[str] = field(default_factory=list)
    iteration: int = 0
    band: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "emit": self.emit,
            "suppression_reason": self.suppression_reason.value if self.suppression_reason else None,
            "suppression_detail": self.suppression_detail,
            "evidence_text_len": len(self.evidence_text),
            "primary_edge_file": self.primary_edge_file,
            "next_action_type": self.next_action_type,
            "next_action_file": self.next_action_file,
            "evidence_items": list(self.evidence_items),
            "target_file": self.target_file,
            "target_functions": list(self.target_functions),
            "iteration": self.iteration,
            "band": self.band,
            "confidence": self.confidence,
        }


__all__ = ["EmissionKind", "RouterEmission", "SuppressionReason"]
