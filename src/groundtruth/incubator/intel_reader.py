"""RepoIntelReader — decision-time summary queries.

Reads from the 4 summary tables populated by RepoIntelLogger.
Only constructed when GT_ENABLE_REPO_INTEL_DECISIONS=1 (which requires LOGGING).

All queries use ORDER BY with deterministic tiebreakers and LIMIT
to prevent unbounded reads and ensure stable output.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from groundtruth.utils.logger import get_logger

log = get_logger("incubator.intel_reader")


class RepoIntelReader:
    """Read summary tables for decision-time enrichment."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_obligation_history(
        self, subjects: list[str], limit: int = 50
    ) -> list[dict[str, Any]]:
        """Query repo_obligation_stats for historical obligation patterns.

        Returns subjects that have been seen before, with their counts
        and average confidence. Deterministic ordering.
        """
        if not subjects:
            return []
        try:
            placeholders = ",".join("?" for _ in subjects)
            cursor = self._conn.execute(
                f"""SELECT subject, obligation_kind, seen_count, confidence_avg, last_seen_at
                    FROM repo_obligation_stats
                    WHERE subject IN ({placeholders})
                    ORDER BY seen_count DESC, subject, obligation_kind
                    LIMIT ?""",
                (*subjects, limit),
            )
            return [
                {
                    "subject": row["subject"],
                    "obligation_kind": row["obligation_kind"],
                    "seen_count": row["seen_count"],
                    "confidence_avg": round(row["confidence_avg"], 3)
                    if row["confidence_avg"] else None,
                    "last_seen_at": row["last_seen_at"],
                }
                for row in cursor.fetchall()
            ]
        except sqlite3.Error:
            log.debug("intel_read_failed", query="obligation_history", exc_info=True)
            return []

    def get_convention_stability(self, scope_key: str) -> dict[str, Any] | None:
        """Is this scope's convention stable or drifting?

        Returns None if no data for this scope.
        """
        try:
            cursor = self._conn.execute(
                """SELECT scope_key, fingerprint_hash, stable_count, drift_count, last_seen_at
                   FROM repo_convention_stats
                   WHERE scope_key = ?""",
                (scope_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            total = row["stable_count"] + row["drift_count"]
            stability = row["stable_count"] / total if total > 0 else 0.0
            return {
                "scope_key": row["scope_key"],
                "fingerprint_hash": row["fingerprint_hash"],
                "stable_count": row["stable_count"],
                "drift_count": row["drift_count"],
                "stability_rate": round(stability, 3),
                "last_seen_at": row["last_seen_at"],
            }
        except sqlite3.Error:
            log.debug("intel_read_failed", query="convention_stability", exc_info=True)
            return None

    def get_confusion_rate(self, symbol: str) -> float:
        """How often is this symbol confused by agents?

        Returns 0.0 if no data. Higher = more frequently confused.
        """
        try:
            cursor = self._conn.execute(
                """SELECT SUM(seen_count) as total
                   FROM repo_confusion_stats
                   WHERE symbol = ?""",
                (symbol,),
            )
            row = cursor.fetchone()
            return float(row["total"]) if row and row["total"] else 0.0
        except sqlite3.Error:
            return 0.0

    def get_cochange_partners(
        self, file_path: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Files that frequently change together with the given file.

        Deterministic ordering by seen_count DESC.
        """
        try:
            cursor = self._conn.execute(
                """SELECT file_a, file_b, seen_count, last_seen_at
                   FROM repo_cochange
                   WHERE file_a = ? OR file_b = ?
                   ORDER BY seen_count DESC, file_a, file_b
                   LIMIT ?""",
                (file_path, file_path, limit),
            )
            results = []
            for row in cursor.fetchall():
                partner = row["file_b"] if row["file_a"] == file_path else row["file_a"]
                results.append({
                    "partner": partner,
                    "seen_count": row["seen_count"],
                    "last_seen_at": row["last_seen_at"],
                })
            return results
        except sqlite3.Error:
            log.debug("intel_read_failed", query="cochange_partners", exc_info=True)
            return []
