"""Freshness tracking — detect when index entries are stale.

Correct-but-stale index = wrong obligations. If a file was modified after
its last indexing, any facts derived from it may be outdated. This module
compares file mtimes against stored index timestamps to classify freshness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class FreshnessLevel(Enum):
    """How current the index entry is relative to the file on disk."""

    FRESH = "fresh"
    SLIGHTLY_STALE = "slightly_stale"
    STALE = "stale"


@dataclass
class FreshnessResult:
    """Freshness assessment for a single file."""

    file_path: str
    level: FreshnessLevel
    last_indexed_at: int | None  # unix timestamp from DB
    last_modified_at: float | None  # file mtime
    staleness_seconds: float | None  # mtime - last_indexed_at


class FreshnessChecker:
    """Checks whether index entries are fresh relative to file modification times."""

    def __init__(
        self,
        fresh_threshold_seconds: float = 60.0,  # <1 min = fresh
        stale_threshold_seconds: float = 3600.0,  # >1 hour = stale
    ) -> None:
        self.fresh_threshold_seconds = fresh_threshold_seconds
        self.stale_threshold_seconds = stale_threshold_seconds

    def check_file(self, file_path: str, last_indexed_at: int | None) -> FreshnessResult:
        """Check if a file's index entry is fresh."""
        if last_indexed_at is None:
            return FreshnessResult(
                file_path=file_path,
                level=FreshnessLevel.STALE,
                last_indexed_at=None,
                last_modified_at=None,
                staleness_seconds=None,
            )

        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            # File doesn't exist or can't be read — check which case
            if not os.path.exists(file_path):
                return FreshnessResult(
                    file_path=file_path,
                    level=FreshnessLevel.STALE,
                    last_indexed_at=last_indexed_at,
                    last_modified_at=None,
                    staleness_seconds=None,
                )
            # OS error but file exists — uncertain
            return FreshnessResult(
                file_path=file_path,
                level=FreshnessLevel.SLIGHTLY_STALE,
                last_indexed_at=last_indexed_at,
                last_modified_at=None,
                staleness_seconds=None,
            )

        delta = mtime - last_indexed_at
        if delta <= self.fresh_threshold_seconds:
            level = FreshnessLevel.FRESH
        elif delta <= self.stale_threshold_seconds:
            level = FreshnessLevel.SLIGHTLY_STALE
        else:
            level = FreshnessLevel.STALE

        return FreshnessResult(
            file_path=file_path,
            level=level,
            last_indexed_at=last_indexed_at,
            last_modified_at=mtime,
            staleness_seconds=delta,
        )

    def check_files(
        self, file_entries: list[tuple[str, int | None]]
    ) -> list[FreshnessResult]:
        """Check freshness for multiple files."""
        return [self.check_file(path, ts) for path, ts in file_entries]

    def overall_freshness(self, results: list[FreshnessResult]) -> FreshnessLevel:
        """Overall freshness: worst of all files. If any STALE -> STALE."""
        if not results:
            return FreshnessLevel.FRESH
        if any(r.level == FreshnessLevel.STALE for r in results):
            return FreshnessLevel.STALE
        if any(r.level == FreshnessLevel.SLIGHTLY_STALE for r in results):
            return FreshnessLevel.SLIGHTLY_STALE
        return FreshnessLevel.FRESH


def to_trust_tier(level: FreshnessLevel) -> str:
    """Map freshness level to a trust advisory string."""
    if level == FreshnessLevel.FRESH:
        return "does not affect trust"
    if level == FreshnessLevel.SLIGHTLY_STALE:
        return "may affect trust"
    return "should downgrade trust"
