"""Record every delivery/suppression decision.

Every GT signal must end in exactly one terminal state.
No silent drops — if a signal is not delivered, there must be a logged reason.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Canonical durable-sink defaults (W1a): the mini seam's _ledger_line_direct used
# GT_RUNTIME_LEDGER / /tmp/gt_runtime_ledger.jsonl; this module is now the canonical
# home of that semantics and the mini adopts append_ledger_line in W2.
DEFAULT_SINK_ENV = "GT_RUNTIME_LEDGER"
DEFAULT_SINK_PATH = "/tmp/gt_runtime_ledger.jsonl"


def resolve_sink_path(explicit: str | None = None) -> str:
    """The durable-ledger file path: an explicit argument wins, else the
    ``GT_RUNTIME_LEDGER`` env var, else :data:`DEFAULT_SINK_PATH`."""
    if explicit:
        return explicit
    return os.environ.get(DEFAULT_SINK_ENV, DEFAULT_SINK_PATH)


def append_ledger_line(entry: dict[str, Any], path: str) -> None:
    """SIGKILL-safe append of ONE JSON line to the durable ledger FILE.

    Durability by construction: the record is APPENDED (never a truncate-then-rewrite,
    which has a window where a mid-write SIGKILL blanks the whole file) and the write
    completes before the ``with`` block closes the handle, so the bytes reach the OS
    the instant the record is made and survive process death. A ``timestamp_ms`` is
    injected when absent (schema parity with :meth:`LedgerEntry.to_dict`). Best-effort:
    telemetry must NEVER break the agent loop, so every I/O error is swallowed.

    This is the canonical, standalone home of the semantics
    ``gt_mini_patch._ledger_line_direct`` provides; the mini seam adopts THIS in W2."""
    try:
        if "timestamp_ms" not in entry:
            try:
                entry = {**entry, "timestamp_ms": int(time.time() * 1000)}
            except Exception:  # noqa: BLE001
                entry = {**entry, "timestamp_ms": 0}
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the agent loop
        pass


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
    """Append-only log of every GT signal decision.

    An optional durable file sink (``sink_path``) makes each recorded entry survive a
    mid-run SIGKILL: on :meth:`record` the entry is ALSO appended as one JSON line to
    the sink via :func:`append_ledger_line`. When ``sink_path`` is ``None`` (the
    default) the Ledger is exactly its prior in-memory-only self — byte-identical."""

    def __init__(self, sink_path: str | None = None) -> None:
        self._entries: list[LedgerEntry] = []
        self._sink_path = sink_path

    def record(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)
        if self._sink_path:
            append_ledger_line(entry.to_dict(), self._sink_path)

    def delivered(
        self, layer: str, event_type: str, file_path: str, chars: int, iteration: int = 0
    ) -> None:
        self.record(
            LedgerEntry(
                layer=layer,
                event_type=event_type,
                file_path=file_path,
                outcome=SignalOutcome.DELIVERED,
                chars_delivered=chars,
                iteration=iteration,
            )
        )

    def suppressed(
        self,
        layer: str,
        event_type: str,
        file_path: str,
        outcome: SignalOutcome,
        reason: str = "",
        iteration: int = 0,
    ) -> None:
        self.record(
            LedgerEntry(
                layer=layer,
                event_type=event_type,
                file_path=file_path,
                outcome=outcome,
                reason=reason,
                iteration=iteration,
            )
        )

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
