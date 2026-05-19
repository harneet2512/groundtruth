"""Structured trace fields and suppression reasons for GT mechanism diagnostics.

Every GT mechanism emission or suppression should include these fields in its
[GT_META] log line. This enables systematic diagnosis across runs.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any


class SuppressionReason(str, enum.Enum):
    """Why a mechanism was suppressed (not emitted to the agent)."""

    NONE = "NONE"
    NO_GRAPH_DB = "NO_GRAPH_DB"
    INDEX_NOT_READY = "INDEX_NOT_READY"
    NO_EVIDENCE = "NO_EVIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    TOO_LATE = "TOO_LATE"
    BUDGET = "BUDGET"
    NOT_AGENT_VISIBLE = "NOT_AGENT_VISIBLE"
    IMPORT_ERROR = "IMPORT_ERROR"
    SNIPPET_ERROR = "SNIPPET_ERROR"
    DISABLED = "DISABLED"
    GATE_MISMATCH = "GATE_MISMATCH"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass
class TraceEvent:
    """Structured trace for one mechanism emission/suppression."""

    run_id: str = ""
    task_id: str = ""
    mechanism: str = ""
    layer: str = ""
    event_type: str = ""
    step: int = 0
    timestamp: float = field(default_factory=time.time)

    graph_db_exists: bool = False
    index_ready: bool = False
    evidence_count: int = 0
    confidence: float = 0.0

    emit_or_suppress: str = "suppress"
    suppression_reason: SuppressionReason = SuppressionReason.NONE

    agent_visible: bool = False
    delivery_surface: str = ""
    payload_hash: str = ""
    payload_tokens: int = 0
    remaining_turns: int = 0

    def to_log_line(self) -> str:
        """Format as a single [GT_TRACE] log line for grep-ability."""
        parts = [
            f"mech={self.mechanism}",
            f"layer={self.layer}",
            f"event={self.event_type}",
            f"step={self.step}",
            f"graph_db={self.graph_db_exists}",
            f"evidence={self.evidence_count}",
            f"conf={self.confidence:.2f}",
            f"action={self.emit_or_suppress}",
        ]
        if self.emit_or_suppress == "suppress":
            parts.append(f"reason={self.suppression_reason.value}")
        parts.extend([
            f"visible={self.agent_visible}",
            f"surface={self.delivery_surface}",
            f"tokens={self.payload_tokens}",
            f"turns_left={self.remaining_turns}",
        ])
        if self.task_id:
            parts.insert(0, f"task={self.task_id}")
        return "[GT_TRACE] " + " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mechanism": self.mechanism,
            "layer": self.layer,
            "event_type": self.event_type,
            "step": self.step,
            "timestamp": self.timestamp,
            "graph_db_exists": self.graph_db_exists,
            "index_ready": self.index_ready,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "emit_or_suppress": self.emit_or_suppress,
            "suppression_reason": self.suppression_reason.value,
            "agent_visible": self.agent_visible,
            "delivery_surface": self.delivery_surface,
            "payload_hash": self.payload_hash,
            "payload_tokens": self.payload_tokens,
            "remaining_turns": self.remaining_turns,
        }
        return d
