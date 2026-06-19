"""Evidence helpers — create and truncate evidence items."""

from __future__ import annotations

from typing import Any

from .schemas import EvidenceItem, EvidenceKind


_PRIORITY_ORDER = [
    EvidenceKind.L3_CALLER_CODE.value,
    EvidenceKind.L3_CONTRACT.value,
    EvidenceKind.L3_TEST_ASSERTION.value,
    EvidenceKind.L3_TARGETED_VERIFICATION.value,
    EvidenceKind.L3_SIGNATURE.value,
    EvidenceKind.L3_SIBLING_PATTERN.value,
    EvidenceKind.L3B_CALLER_EDGE.value,
    EvidenceKind.L3B_CALLEE_EDGE.value,
    EvidenceKind.L3B_IMPORTER_EDGE.value,
    EvidenceKind.L1_CANDIDATE.value,
    EvidenceKind.L4_GIT_PRECEDENT.value,
    EvidenceKind.L4_CONSTRAINT.value,
    EvidenceKind.L5_EVENT_DETECTED.value,
    EvidenceKind.L5B_INTERVENTION.value,
    EvidenceKind.L6_REINDEX.value,
    EvidenceKind.HYGIENE_STRIP.value,
]


def make_evidence_item(
    kind: str,
    file_path: str | None = None,
    symbol: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    text: str | None = None,
    confidence: float | None = None,
    source: str | None = None,
    resolution_method: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a validated EvidenceItem dict."""
    item = EvidenceItem(
        kind=kind,
        file_path=file_path,
        symbol=symbol,
        line_start=line_start,
        line_end=line_end,
        text=text,
        confidence=confidence,
        source=source,
        resolution_method=resolution_method,
        reason=reason,
    )
    return item.to_dict()


def truncate_evidence_by_priority(
    items: list[dict[str, Any]],
    max_tokens: int,
    priority_order: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Truncate evidence items to fit within token cap.

    Returns (kept_items, truncation_reason or None).
    """
    if not items:
        return [], None

    order = priority_order or _PRIORITY_ORDER

    def _priority(item: dict[str, Any]) -> int:
        kind = item.get("kind", "")
        try:
            return order.index(kind)
        except ValueError:
            return len(order)

    sorted_items = sorted(items, key=_priority)

    kept: list[dict[str, Any]] = []
    total_tokens = 0
    cut_kinds: list[str] = []

    for item in sorted_items:
        item_tokens = item.get("token_estimate", 0) or 0
        if item_tokens == 0 and item.get("text"):
            item_tokens = max(1, len(item["text"]) // 4)
        if total_tokens + item_tokens <= max_tokens:
            kept.append(item)
            total_tokens += item_tokens
        else:
            cut_kinds.append(item.get("kind", "unknown"))

    if cut_kinds:
        reason = f"truncated {len(cut_kinds)} items ({', '.join(set(cut_kinds))}) to fit {max_tokens} token cap"
        return kept, reason

    return kept, None
