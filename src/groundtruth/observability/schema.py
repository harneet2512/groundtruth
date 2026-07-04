"""Trace schema for endpoint synthesis observability.

Each endpoint call produces one EndpointTrace with four layers:
  A. RequestLayer — what came in
  B. ComponentTrace[] — what each internal module did
  C. SynthesisLayer — what was included/excluded and why
  D. ResponseLayer — what went out
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class ComponentStatus(enum.Enum):
    """Status of an internal component during endpoint execution."""

    USED = "used"
    SKIPPED = "skipped"
    ABSTAINED = "abstained"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


@dataclass
class RequestLayer:
    """A. What came in."""

    endpoint: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    input_summary: str = ""
    symbol: str | None = None
    file_path: str | None = None
    patch_summary: str | None = None
    task_context: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComponentTrace:
    """B. What one internal module did."""

    component: str
    status: ComponentStatus
    output_summary: str = ""
    confidence: float | None = None
    trust_tier: str | None = None
    reason: str = ""
    duration_ms: float = 0.0
    item_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SynthesisLayer:
    """C. What was included/excluded in the final answer."""

    included: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    exclusion_reasons: dict[str, str] = field(default_factory=dict)
    verdict: str = ""
    ranking_summary: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponseLayer:
    """D. What went out."""

    response_type: str = ""
    item_count: int = 0
    verdict: str = ""
    next_step: str = ""
    output_summary: str = ""
    total_duration_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EndpointTrace:
    """One complete trace record for a single endpoint call."""

    request: RequestLayer
    components: list[ComponentTrace] = field(default_factory=list)
    synthesis: SynthesisLayer = field(default_factory=SynthesisLayer)
    response: ResponseLayer = field(default_factory=ResponseLayer)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "request": {
                "trace_id": self.request.trace_id,
                "timestamp": self.request.timestamp,
                "endpoint": self.request.endpoint,
                "input_summary": self.request.input_summary,
                "symbol": self.request.symbol,
                "file_path": self.request.file_path,
                "patch_summary": self.request.patch_summary,
                "task_context": self.request.task_context,
                **({} if not self.request.extra else {"extra": self.request.extra}),
            },
            "components": [
                {
                    "component": c.component,
                    "status": c.status.value,
                    "output_summary": c.output_summary,
                    **({"confidence": c.confidence} if c.confidence is not None else {}),
                    **({"trust_tier": c.trust_tier} if c.trust_tier else {}),
                    **({"reason": c.reason} if c.reason else {}),
                    "duration_ms": round(c.duration_ms, 1),
                    **({"item_count": c.item_count} if c.item_count else {}),
                    **({} if not c.extra else {"extra": c.extra}),
                }
                for c in self.components
            ],
            "synthesis": {
                "included": self.synthesis.included,
                "excluded": self.synthesis.excluded,
                **(
                    {"exclusion_reasons": self.synthesis.exclusion_reasons}
                    if self.synthesis.exclusion_reasons
                    else {}
                ),
                "verdict": self.synthesis.verdict,
                **(
                    {"ranking_summary": self.synthesis.ranking_summary}
                    if self.synthesis.ranking_summary
                    else {}
                ),
                **({} if not self.synthesis.extra else {"extra": self.synthesis.extra}),
            },
            "response": {
                "response_type": self.response.response_type,
                "item_count": self.response.item_count,
                "verdict": self.response.verdict,
                **({"next_step": self.response.next_step} if self.response.next_step else {}),
                "output_summary": self.response.output_summary,
                "total_duration_ms": round(self.response.total_duration_ms, 1),
                **({} if not self.response.extra else {"extra": self.response.extra}),
            },
        }
