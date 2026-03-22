"""RepoIntelLogger — append-only data collection to summary tables.

Writes to 4 summary tables (obligation_stats, convention_stats,
confusion_stats, cochange). Tables are created lazily on first write.

Relationship to pattern_log: pattern_log is legacy raw telemetry.
This logger writes to NEW summary tables only. Both coexist.

Gated by GT_ENABLE_REPO_INTEL_LOGGING. When OFF, nothing is constructed.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from groundtruth.utils.logger import get_logger

log = get_logger("incubator.intel_logger")

_SUMMARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_obligation_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    obligation_kind TEXT NOT NULL,
    seen_count INTEGER DEFAULT 1,
    confidence_avg REAL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(subject, obligation_kind)
);

CREATE TABLE IF NOT EXISTS repo_convention_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    fingerprint_hash TEXT NOT NULL,
    stable_count INTEGER DEFAULT 1,
    drift_count INTEGER DEFAULT 0,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(scope_key)
);

CREATE TABLE IF NOT EXISTS repo_confusion_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    confusion_kind TEXT NOT NULL,
    seen_count INTEGER DEFAULT 1,
    corrected_count INTEGER DEFAULT 0,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(symbol, confusion_kind)
);

CREATE TABLE IF NOT EXISTS repo_cochange (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_a TEXT NOT NULL,
    file_b TEXT NOT NULL,
    seen_count INTEGER DEFAULT 1,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(file_a, file_b)
);
"""


class RepoIntelLogger:
    """Append-only intelligence logger.

    Extracts obligations, contradictions, and changed files from tool results
    and upserts into summary tables. Single transaction per record() call.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._schema_created = False

    def _ensure_schema(self) -> None:
        """Create summary tables lazily on first write."""
        if self._schema_created:
            return
        self._conn.executescript(_SUMMARY_SCHEMA)
        self._schema_created = True

    def record(self, tool_name: str, result: dict[str, Any]) -> None:
        """Extract and log intelligence from a tool result.

        Called after token tracking — result shape matches what agent sees.
        Single transaction, <1ms on typical results.
        """
        try:
            self._ensure_schema()
            now = int(time.time())

            # Log obligations
            obligations = result.get("obligations", [])
            if obligations:
                self._log_obligations(obligations, now)

            # Log contradictions
            contradictions = result.get("contradictions", [])
            if contradictions:
                self._log_contradictions(contradictions, now)

            # Log co-changes (files that changed together)
            changed_files = self._extract_changed_files(result)
            if len(changed_files) >= 2:
                self._log_cochanges(changed_files, now)

            self._conn.commit()
        except sqlite3.Error:
            log.debug("intel_log_failed", tool=tool_name, exc_info=True)

    def _log_obligations(
        self, obligations: list[dict[str, Any]], now: int
    ) -> None:
        """Upsert obligation patterns into repo_obligation_stats."""
        for obl in obligations[:20]:
            subject = obl.get("target", "") or obl.get("source", "")
            kind = obl.get("kind", "unknown")
            confidence = obl.get("confidence", 0.0)
            if not subject:
                continue
            self._conn.execute(
                """INSERT INTO repo_obligation_stats
                   (subject, obligation_kind, seen_count, confidence_avg, last_seen_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(subject, obligation_kind) DO UPDATE SET
                       seen_count = seen_count + 1,
                       confidence_avg = (confidence_avg * (seen_count - 1) + ?) / seen_count,
                       last_seen_at = ?""",
                (subject, kind, confidence, now, confidence, now),
            )

    def _log_contradictions(
        self, contradictions: list[dict[str, Any]], now: int
    ) -> None:
        """Log contradiction symbols as confusion candidates."""
        for c in contradictions[:10]:
            symbol = c.get("file", "") or c.get("symbol", "")
            kind = c.get("kind", "unknown")
            if not symbol:
                continue
            self._conn.execute(
                """INSERT INTO repo_confusion_stats
                   (symbol, confusion_kind, seen_count, corrected_count, last_seen_at)
                   VALUES (?, ?, 1, 0, ?)
                   ON CONFLICT(symbol, confusion_kind) DO UPDATE SET
                       seen_count = seen_count + 1,
                       last_seen_at = ?""",
                (symbol, kind, now, now),
            )

    def _log_cochanges(self, files: list[str], now: int) -> None:
        """Log file pairs that changed together."""
        sorted_files = sorted(set(files))
        for i, file_a in enumerate(sorted_files):
            for file_b in sorted_files[i + 1:]:
                self._conn.execute(
                    """INSERT INTO repo_cochange
                       (file_a, file_b, seen_count, last_seen_at)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(file_a, file_b) DO UPDATE SET
                           seen_count = seen_count + 1,
                           last_seen_at = ?""",
                    (file_a, file_b, now, now),
                )

    @staticmethod
    def _extract_changed_files(result: dict[str, Any]) -> list[str]:
        """Extract changed file paths from a tool result."""
        files: list[str] = []
        # From obligations
        for obl in result.get("obligations", []):
            f = obl.get("file", "")
            if f:
                files.append(f)
        # From contradictions
        for c in result.get("contradictions", []):
            f = c.get("file", "")
            if f:
                files.append(f)
        return files
