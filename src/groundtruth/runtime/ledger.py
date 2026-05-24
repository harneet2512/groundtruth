"""Record every delivery/suppression decision.

Every GT signal must end in exactly one terminal state.
No silent drops — if a signal is not delivered, there must be a logged reason.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SignalOutcome(str, Enum):
    DELIVERED = "delivered"
    SUPPRESSED_BUDGET = "suppressed_budget"
    SUPPRESSED_DUPLICATE = "suppressed_duplicate"
    SUPPRESSED_NO_MARKERS = "suppressed_no_markers"
    SUPPRESSED_HIDDEN_ONLY = "suppressed_hidden_only"
    SUPPRESSED_LOW_PROVENANCE = "suppressed_low_confidence_or_low_provenance"
    SUPPRESSED_NOT_RELEVANT = "suppressed_not_relevant"
    SUPPRESSED_WRONG_PHASE = "suppressed_wrong_phase"
    SUPPRESSED_INTERNAL_ONLY = "suppressed_internal_only"
    SUPPRESSED_DISABLED = "suppressed_disabled"
    PROVIDER_FAILED = "provider_failed"
    ROUTER_SHADOW_ONLY = "router_shadow_only"


@dataclass
class LedgerEntry:
    layer: str
    event_type: str
    file_path: str
    outcome: SignalOutcome
    reason: str = ""
    chars_delivered: int = 0
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    iteration: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "event_type": self.event_type,
            "file_path": self.file_path,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "chars_delivered": self.chars_delivered,
            "timestamp_ms": self.timestamp_ms,
            "iteration": self.iteration,
        }


class Ledger:
    """Append-only log of every GT signal decision."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def record(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)

    def delivered(self, layer: str, event_type: str, file_path: str,
                  chars: int, iteration: int = 0) -> None:
        self.record(LedgerEntry(
            layer=layer, event_type=event_type, file_path=file_path,
            outcome=SignalOutcome.DELIVERED, chars_delivered=chars,
            iteration=iteration,
        ))

    def suppressed(self, layer: str, event_type: str, file_path: str,
                   outcome: SignalOutcome, reason: str = "",
                   iteration: int = 0) -> None:
        self.record(LedgerEntry(
            layer=layer, event_type=event_type, file_path=file_path,
            outcome=outcome, reason=reason, iteration=iteration,
        ))

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self._entries:
            counts[e.outcome.value] = counts.get(e.outcome.value, 0) + 1
        return counts

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e.to_dict()) for e in self._entries)

    def has_silent_drops(self) -> bool:
        """True if any entry has no outcome — should never happen."""
        return any(not e.outcome for e in self._entries)
