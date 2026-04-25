"""Tests for signal pruning — confidence floor, novelty gating, per-kind cap."""

from __future__ import annotations

from groundtruth.schema.finding import (
    Finding,
    FindingKind,
    Location,
    Severity,
)
from groundtruth.schema.pruning import prune_findings


def _make(
    kind: FindingKind = FindingKind.GUARD_REMOVED,
    confidence: float = 0.8,
    novelty: bool = True,
    line: int = 1,
) -> Finding:
    return Finding(
        kind=kind,
        severity=Severity.WARNING,
        confidence=confidence,
        location=Location(file="x.py", line=line),
        message="test",
        novelty=novelty,
    )


class TestPruneFindings:
    def test_passes_above_floor(self) -> None:
        findings = [_make(confidence=0.8)]
        result = prune_findings(findings)
        assert len(result) == 1

    def test_drops_below_floor(self) -> None:
        findings = [_make(confidence=0.5)]
        result = prune_findings(findings)
        assert len(result) == 0

    def test_drops_at_floor_boundary(self) -> None:
        findings = [_make(confidence=0.59)]
        result = prune_findings(findings)
        assert len(result) == 0

    def test_keeps_at_floor(self) -> None:
        findings = [_make(confidence=0.6)]
        result = prune_findings(findings)
        assert len(result) == 1

    def test_custom_floor(self) -> None:
        findings = [_make(confidence=0.4)]
        result = prune_findings(findings, confidence_floor=0.3)
        assert len(result) == 1

    def test_drops_not_novel(self) -> None:
        findings = [_make(novelty=False)]
        result = prune_findings(findings)
        assert len(result) == 0

    def test_keeps_novel(self) -> None:
        findings = [_make(novelty=True)]
        result = prune_findings(findings)
        assert len(result) == 1

    def test_per_kind_cap(self) -> None:
        findings = [_make(line=i) for i in range(5)]
        result = prune_findings(findings, max_per_kind=3)
        assert len(result) == 3

    def test_per_kind_cap_independent(self) -> None:
        findings = [
            *[_make(kind=FindingKind.GUARD_REMOVED, line=i) for i in range(5)],
            *[_make(kind=FindingKind.CALLER_CONTRACT, line=i + 10) for i in range(5)],
        ]
        result = prune_findings(findings, max_per_kind=3)
        guard_count = sum(1 for f in result if f.kind == FindingKind.GUARD_REMOVED)
        caller_count = sum(1 for f in result if f.kind == FindingKind.CALLER_CONTRACT)
        assert guard_count == 3
        assert caller_count == 3

    def test_sorts_by_confidence_descending(self) -> None:
        findings = [
            _make(confidence=0.7, line=1),
            _make(confidence=0.9, line=2),
            _make(confidence=0.8, line=3),
        ]
        result = prune_findings(findings, max_per_kind=2)
        assert len(result) == 2
        assert result[0].confidence == 0.9
        assert result[1].confidence == 0.8

    def test_empty_input(self) -> None:
        assert prune_findings([]) == []

    def test_combined_filters(self) -> None:
        findings = [
            _make(confidence=0.9, novelty=True, line=1),   # keep
            _make(confidence=0.5, novelty=True, line=2),   # drop (below floor)
            _make(confidence=0.9, novelty=False, line=3),  # drop (not novel)
            _make(confidence=0.8, novelty=True, line=4),   # keep
        ]
        result = prune_findings(findings)
        assert len(result) == 2
        assert all(f.confidence >= 0.6 for f in result)
        assert all(f.novelty for f in result)
