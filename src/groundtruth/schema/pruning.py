"""Signal pruning — confidence floor and novelty gating."""

from __future__ import annotations

from groundtruth.schema.finding import Finding


def prune_findings(
    findings: list[Finding],
    *,
    confidence_floor: float = 0.7,
    max_per_kind: int = 3,
) -> list[Finding]:
    """Remove low-value findings.

    - Drop below confidence floor.
    - Drop already-shown (novelty=False).
    - Cap at max_per_kind per FindingKind (AutoCodeRover pattern).
    """
    kind_counts: dict[str, int] = {}
    result: list[Finding] = []
    for f in sorted(findings, key=lambda x: x.confidence, reverse=True):
        if f.confidence < confidence_floor:
            continue
        if not f.novelty:
            continue
        kind_key = f.kind.value
        count = kind_counts.get(kind_key, 0)
        if count >= max_per_kind:
            continue
        kind_counts[kind_key] = count + 1
        result.append(f)
    return result
