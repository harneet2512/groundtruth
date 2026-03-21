"""Tests for freshness/staleness tracking."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from groundtruth.index.freshness import (
    FreshnessChecker,
    FreshnessLevel,
    FreshnessResult,
    to_trust_tier,
)


@pytest.fixture
def checker() -> FreshnessChecker:
    return FreshnessChecker(fresh_threshold_seconds=60.0, stale_threshold_seconds=3600.0)


def _write_file(tmp_path: object, name: str) -> str:
    """Create a file and return its path."""
    p = tmp_path / name  # type: ignore[operator]
    p.write_text("content")
    return str(p)


class TestCheckFile:
    def test_fresh_file_indexed_recently(self, checker: FreshnessChecker, tmp_path: object) -> None:
        """File indexed at its current mtime → FRESH."""
        path = _write_file(tmp_path, "a.py")
        mtime = os.path.getmtime(path)
        # Index timestamp = mtime (delta = 0)
        result = checker.check_file(path, int(mtime))
        assert result.level == FreshnessLevel.FRESH
        assert result.staleness_seconds is not None
        assert result.staleness_seconds <= 60.0

    def test_fresh_file_modified_within_threshold(
        self, checker: FreshnessChecker, tmp_path: object
    ) -> None:
        """File modified slightly after indexing, within fresh threshold → FRESH."""
        path = _write_file(tmp_path, "b.py")
        mtime = os.path.getmtime(path)
        # Indexed 30 seconds before mtime
        result = checker.check_file(path, int(mtime) - 30)
        assert result.level == FreshnessLevel.FRESH

    def test_slightly_stale_file(self, checker: FreshnessChecker, tmp_path: object) -> None:
        """File modified after indexing, beyond fresh but within stale threshold."""
        path = _write_file(tmp_path, "c.py")
        mtime = os.path.getmtime(path)
        # Indexed 600 seconds before mtime (10 minutes)
        result = checker.check_file(path, int(mtime) - 600)
        assert result.level == FreshnessLevel.SLIGHTLY_STALE
        assert result.staleness_seconds is not None
        assert 60.0 < result.staleness_seconds <= 3600.0

    def test_stale_file(self, checker: FreshnessChecker, tmp_path: object) -> None:
        """File modified long after indexing → STALE."""
        path = _write_file(tmp_path, "d.py")
        mtime = os.path.getmtime(path)
        # Indexed 7200 seconds before mtime (2 hours)
        result = checker.check_file(path, int(mtime) - 7200)
        assert result.level == FreshnessLevel.STALE
        assert result.staleness_seconds is not None
        assert result.staleness_seconds > 3600.0

    def test_file_does_not_exist(self, checker: FreshnessChecker) -> None:
        """Deleted file → STALE."""
        result = checker.check_file("/nonexistent/path/x.py", int(time.time()))
        assert result.level == FreshnessLevel.STALE
        assert result.last_modified_at is None

    def test_never_indexed(self, checker: FreshnessChecker, tmp_path: object) -> None:
        """last_indexed_at is None → STALE."""
        path = _write_file(tmp_path, "e.py")
        result = checker.check_file(path, None)
        assert result.level == FreshnessLevel.STALE
        assert result.last_indexed_at is None
        assert result.staleness_seconds is None

    def test_os_error_reading_mtime(self, checker: FreshnessChecker, tmp_path: object) -> None:
        """OS error when reading mtime but file exists → SLIGHTLY_STALE."""
        path = _write_file(tmp_path, "f.py")
        with patch("groundtruth.index.freshness.os.path.getmtime", side_effect=OSError("perm")):
            with patch("groundtruth.index.freshness.os.path.exists", return_value=True):
                result = checker.check_file(path, int(time.time()))
        assert result.level == FreshnessLevel.SLIGHTLY_STALE


class TestCheckFiles:
    def test_mixed_results(self, checker: FreshnessChecker, tmp_path: object) -> None:
        """check_files returns one result per entry."""
        path1 = _write_file(tmp_path, "g.py")
        path2 = _write_file(tmp_path, "h.py")
        mtime1 = os.path.getmtime(path1)
        mtime2 = os.path.getmtime(path2)

        entries: list[tuple[str, int | None]] = [
            (path1, int(mtime1)),       # FRESH
            (path2, int(mtime2) - 7200),  # STALE
        ]
        results = checker.check_files(entries)
        assert len(results) == 2
        assert results[0].level == FreshnessLevel.FRESH
        assert results[1].level == FreshnessLevel.STALE


class TestOverallFreshness:
    def test_all_fresh(self, checker: FreshnessChecker) -> None:
        results = [
            FreshnessResult("a.py", FreshnessLevel.FRESH, 100, 100.0, 0.0),
            FreshnessResult("b.py", FreshnessLevel.FRESH, 100, 100.0, 0.0),
        ]
        assert checker.overall_freshness(results) == FreshnessLevel.FRESH

    def test_one_stale_makes_overall_stale(self, checker: FreshnessChecker) -> None:
        results = [
            FreshnessResult("a.py", FreshnessLevel.FRESH, 100, 100.0, 0.0),
            FreshnessResult("b.py", FreshnessLevel.STALE, 100, 8000.0, 7900.0),
        ]
        assert checker.overall_freshness(results) == FreshnessLevel.STALE

    def test_empty_results(self, checker: FreshnessChecker) -> None:
        assert checker.overall_freshness([]) == FreshnessLevel.FRESH


class TestCustomThresholds:
    def test_tight_thresholds(self, tmp_path: object) -> None:
        """With a 5-second fresh threshold, 30-second delta is SLIGHTLY_STALE."""
        tight = FreshnessChecker(fresh_threshold_seconds=5.0, stale_threshold_seconds=120.0)
        path = _write_file(tmp_path, "i.py")
        mtime = os.path.getmtime(path)
        result = tight.check_file(path, int(mtime) - 30)
        assert result.level == FreshnessLevel.SLIGHTLY_STALE


class TestToTrustTier:
    def test_fresh(self) -> None:
        assert to_trust_tier(FreshnessLevel.FRESH) == "does not affect trust"

    def test_slightly_stale(self) -> None:
        assert to_trust_tier(FreshnessLevel.SLIGHTLY_STALE) == "may affect trust"

    def test_stale(self) -> None:
        assert to_trust_tier(FreshnessLevel.STALE) == "should downgrade trust"
