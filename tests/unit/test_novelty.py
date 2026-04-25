"""Tests for NoveltyFilter — deterministic finding dedup."""

from __future__ import annotations

from groundtruth.schema.finding import (
    Finding,
    FindingKind,
    Location,
    Severity,
)
from groundtruth.schema.novelty import NoveltyFilter


def _make(kind: FindingKind = FindingKind.GUARD_REMOVED, file: str = "x.py", line: int = 1) -> Finding:
    return Finding(
        kind=kind,
        severity=Severity.WARNING,
        confidence=0.8,
        location=Location(file=file, line=line),
        message="test",
    )


class TestNoveltyFilter:
    def test_first_finding_is_novel(self) -> None:
        nf = NoveltyFilter()
        findings = nf.filter([_make()])
        assert len(findings) == 1
        assert findings[0].novelty is True

    def test_duplicate_is_not_novel(self) -> None:
        nf = NoveltyFilter()
        nf.filter([_make()])
        findings = nf.filter([_make()])
        assert findings[0].novelty is False

    def test_different_kind_is_novel(self) -> None:
        nf = NoveltyFilter()
        nf.filter([_make(FindingKind.GUARD_REMOVED)])
        findings = nf.filter([_make(FindingKind.CALLER_CONTRACT)])
        assert findings[0].novelty is True

    def test_different_file_is_novel(self) -> None:
        nf = NoveltyFilter()
        nf.filter([_make(file="a.py")])
        findings = nf.filter([_make(file="b.py")])
        assert findings[0].novelty is True

    def test_different_line_is_novel(self) -> None:
        nf = NoveltyFilter()
        nf.filter([_make(line=1)])
        findings = nf.filter([_make(line=2)])
        assert findings[0].novelty is True

    def test_mixed_batch(self) -> None:
        nf = NoveltyFilter()
        first = [_make(line=1), _make(line=2)]
        nf.filter(first)
        second = [_make(line=1), _make(line=3)]
        results = nf.filter(second)
        assert results[0].novelty is False  # line=1 already shown
        assert results[1].novelty is True   # line=3 is new

    def test_reset_clears_history(self) -> None:
        nf = NoveltyFilter()
        nf.filter([_make()])
        nf.reset()
        findings = nf.filter([_make()])
        assert findings[0].novelty is True

    def test_shown_count(self) -> None:
        nf = NoveltyFilter()
        assert nf.shown_count() == 0
        nf.filter([_make(line=1), _make(line=2)])
        assert nf.shown_count() == 2
        nf.filter([_make(line=1)])  # duplicate
        assert nf.shown_count() == 2

    def test_immutability_original_unchanged(self) -> None:
        """NoveltyFilter returns copies, does not mutate input."""
        nf = NoveltyFilter()
        original = _make()
        assert original.novelty is True
        nf.filter([original])
        results = nf.filter([_make()])  # same fingerprint
        assert results[0].novelty is False
        assert original.novelty is True  # original unchanged
