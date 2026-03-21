"""Micro-evidence formatter — disciplined output with confidence and role grouping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EvidenceItem:
    role: str           # defines | calls | imports | overrides | tests | obligation
    location: str       # file:line
    detail: str         # one-line evidence
    confidence: float   # 0.0-1.0


def format_evidence(items: list[EvidenceItem], max_items: int = 5) -> list[dict[str, str]]:
    """Cap, rank by confidence, group by role."""
    # Sort by confidence descending
    sorted_items = sorted(items, key=lambda x: x.confidence, reverse=True)

    # Take top N
    top = sorted_items[:max_items]

    return [
        {
            "role": item.role,
            "location": item.location,
            "detail": item.detail,
            "confidence": str(item.confidence),
        }
        for item in top
    ]


def add_evidence(result: dict[str, Any], items: list[EvidenceItem], max_items: int = 5) -> dict[str, Any]:
    """Add formatted evidence to an existing tool result dict."""
    if items:
        result["evidence"] = format_evidence(items, max_items)
        result["evidence_total"] = len(items)
    return result
