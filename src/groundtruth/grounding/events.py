"""Causal observability — append-only event log for grounding-gap instrumentation.

Tracks the lifecycle of tool responses: surfaced -> consumed -> outcome.
Answers: was the signal surfaced? Was it read? Was it followed?
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventKind(Enum):
    INTERVENTION = "intervention"
    CONSUMPTION = "consumption"
    OUTCOME = "outcome"


@dataclass(frozen=True)
class GroundingEvent:
    kind: EventKind
    timestamp: float
    session_id: str
    tool_name: str
    content_hash: str
    metadata: dict[str, str | int | float | bool]


def _hash_content(content: str) -> str:
    """SHA-256 of the first 500 chars of content."""
    return hashlib.sha256(content[:500].encode("utf-8")).hexdigest()


def intervention_event(
    session_id: str,
    tool_name: str,
    response_content: str,
    intervention_type: str,
    findings_count: int,
) -> GroundingEvent:
    """Create an event recording that a tool surfaced findings."""
    return GroundingEvent(
        kind=EventKind.INTERVENTION,
        timestamp=time.time(),
        session_id=session_id,
        tool_name=tool_name,
        content_hash=_hash_content(response_content),
        metadata={
            "intervention_type": intervention_type,
            "findings_count": findings_count,
        },
    )


def consumption_event(
    session_id: str,
    tool_name: str,
    content_hash: str,
    was_followed: bool | None,
) -> GroundingEvent:
    """Create an event recording whether a tool response was consumed."""
    meta: dict[str, str | int | float | bool] = {}
    if was_followed is not None:
        meta["was_followed"] = was_followed
    return GroundingEvent(
        kind=EventKind.CONSUMPTION,
        timestamp=time.time(),
        session_id=session_id,
        tool_name=tool_name,
        content_hash=content_hash,
        metadata=meta,
    )


def outcome_event(
    session_id: str,
    tool_name: str,
    content_hash: str,
    outcome: str,
) -> GroundingEvent:
    """Create an event recording the final outcome after a tool response."""
    return GroundingEvent(
        kind=EventKind.OUTCOME,
        timestamp=time.time(),
        session_id=session_id,
        tool_name=tool_name,
        content_hash=content_hash,
        metadata={"outcome": outcome},
    )


class EventLog:
    """Append-only in-memory event log. Never blocks."""

    def __init__(self) -> None:
        self._events: list[GroundingEvent] = []

    def append(self, event: GroundingEvent) -> None:
        """Append an event. Never blocks."""
        self._events.append(event)

    def get_session_events(self, session_id: str) -> list[GroundingEvent]:
        """Get all events for a session."""
        return [e for e in self._events if e.session_id == session_id]

    def get_surfacing_rate(self, session_id: str) -> float:
        """Fraction of interventions that have a corresponding consumption event.

        Returns 0.0 if there are no interventions (avoids division by zero).
        """
        session = self.get_session_events(session_id)
        interventions = [e for e in session if e.kind == EventKind.INTERVENTION]
        if not interventions:
            return 0.0
        intervention_hashes = {e.content_hash for e in interventions}
        consumption_hashes = {
            e.content_hash for e in session if e.kind == EventKind.CONSUMPTION
        }
        matched = intervention_hashes & consumption_hashes
        return len(matched) / len(intervention_hashes)

    def to_dicts(self) -> list[dict[str, Any]]:
        """Serialize all events for export."""
        return [
            {
                "kind": e.kind.value,
                "timestamp": e.timestamp,
                "session_id": e.session_id,
                "tool_name": e.tool_name,
                "content_hash": e.content_hash,
                "metadata": e.metadata,
            }
            for e in self._events
        ]
