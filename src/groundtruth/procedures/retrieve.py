"""Procedure Retrieval — fetches matching procedure cards at runtime.

Given an issue signature, retrieves structured procedures from the store.
Only confidence-gated results (verified/likely) are returned.
"""

from __future__ import annotations

import sqlite3
import json
import logging

from groundtruth.procedures.cluster import ProcedureClusterer
from groundtruth.procedures.models import ProcedureCard

logger = logging.getLogger(__name__)


class ProcedureRetriever:
    """Retrieves procedure cards from the database."""

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        self._conn = db_conn
        self._clusterer = ProcedureClusterer()

    def retrieve(
        self,
        issue_text: str,
        changed_files: list[str] | None = None,
        max_results: int = 3,
    ) -> list[ProcedureCard]:
        """Retrieve matching procedure cards for an issue.

        Args:
            issue_text: The issue description.
            changed_files: Optional files already changed (for context).
            max_results: Maximum procedures to return.

        Returns only verified/likely procedures. Possible tier is suppressed.
        """
        signature = self._clusterer.classify_issue(issue_text)
        return self._query_by_signature(signature, max_results)

    def _query_by_signature(
        self, signature: str, max_results: int
    ) -> list[ProcedureCard]:
        """Query the repair_procedures table."""
        try:
            cursor = self._conn.execute(
                """SELECT * FROM repair_procedures
                   WHERE issue_signature = ? AND confidence >= 0.6
                   ORDER BY confidence DESC
                   LIMIT ?""",
                (signature, max_results),
            )
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            logger.debug("Failed to query procedures: %s", exc)
            return []

        results: list[ProcedureCard] = []
        for row in rows:
            try:
                steps = json.loads(row[3]) if row[3] else []
                anti_patterns = json.loads(row[4]) if row[4] else []
                validation_plan = json.loads(row[5]) if row[5] else []
                confidence = row[6]
                source_count = row[7]

                # Determine tier
                if source_count >= 5 and confidence >= 0.8:
                    tier = "verified"
                elif source_count >= 3:
                    tier = "likely"
                else:
                    tier = "possible"

                # Suppress possible tier
                if tier == "possible":
                    continue

                results.append(ProcedureCard(
                    issue_signature=row[1],
                    procedure_name=row[2],
                    inspection_order=tuple(steps.get("inspection_order", [])) if isinstance(steps, dict) else tuple(steps),
                    co_edit_sets=tuple(tuple(s) for s in steps.get("co_edit_sets", [])) if isinstance(steps, dict) else (),
                    anti_patterns=tuple(anti_patterns),
                    validation_plan=tuple(validation_plan),
                    confidence=confidence,
                    source_count=source_count,
                    tier=tier,
                ))
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                logger.debug("Failed to parse procedure row: %s", exc)
                continue

        return results
