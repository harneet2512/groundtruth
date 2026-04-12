"""Procedure Distillation — offline pipeline to create procedure cards.

Takes raw trajectory logs and distills them into structured procedures.
This runs OFFLINE after benchmark runs, not at runtime.

Pipeline:
1. Parse trajectory logs
2. Cluster by (issue_signature, repair_pattern)
3. For clusters with ≥3 successful examples:
   - Extract common inspection order
   - Extract common co-edit sets
   - Extract anti-patterns from failed trajectories
   - Generate validation plan
4. Persist to repair_procedures table
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections import Counter

from groundtruth.procedures.cluster import ProcedureClusterer, TrajectoryRecord

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_procedures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_signature TEXT NOT NULL,
    procedure_name TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    anti_patterns_json TEXT,
    validation_plan_json TEXT,
    confidence REAL NOT NULL,
    source_count INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(issue_signature, procedure_name)
);

CREATE TABLE IF NOT EXISTS procedure_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER REFERENCES repair_procedures(id),
    task_ref TEXT,
    repo_ref TEXT,
    outcome TEXT
);
"""


class ProcedureDistiller:
    """Offline distillation of trajectories into procedure cards."""

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        self._conn = db_conn
        self._clusterer = ProcedureClusterer()
        self._ensure_schema()

    def distill(self, trajectories: list[TrajectoryRecord]) -> int:
        """Distill trajectories into procedure cards.

        Returns number of procedures created/updated.
        """
        clusters = self._clusterer.cluster(trajectories)
        count = 0

        for signature, trajs in clusters.items():
            successful = [t for t in trajs if t.outcome == "resolved"]
            failed = [t for t in trajs if t.outcome == "failed"]

            if len(successful) < 3:
                continue

            # Extract structured procedure
            inspection_order = self._extract_inspection_order(successful)
            co_edit_sets = self._extract_co_edit_sets(successful)
            anti_patterns = self._extract_anti_patterns(failed)
            validation_plan = self._extract_validation_plan(successful)

            # Compute confidence
            success_rate = len(successful) / len(trajs)
            source_count = len(trajs)
            confidence = success_rate * math.log(max(source_count, 2))
            confidence = min(1.0, confidence)

            # Generate procedure name
            procedure_name = f"repair_{signature.replace(':', '_')}"

            # Persist
            self._persist_procedure(
                signature=signature,
                procedure_name=procedure_name,
                inspection_order=inspection_order,
                co_edit_sets=co_edit_sets,
                anti_patterns=anti_patterns,
                validation_plan=validation_plan,
                confidence=confidence,
                source_count=source_count,
                examples=trajs,
            )
            count += 1

        return count

    def _extract_inspection_order(
        self, successful: list[TrajectoryRecord]
    ) -> list[str]:
        """Extract common inspection order from successful trajectories.

        Looks at which files were visited first, second, third across
        all successful trajectories.
        """
        # Count which file TYPES appear at each position
        position_counts: dict[int, Counter] = {}
        for traj in successful:
            for i, file_path in enumerate(traj.files_visited[:5]):
                pos = min(i, 4)
                if pos not in position_counts:
                    position_counts[pos] = Counter()
                file_type = self._classify_file(file_path)
                position_counts[pos][file_type] += 1

        # Build ordered list from most common at each position
        order: list[str] = []
        for pos in sorted(position_counts.keys()):
            most_common = position_counts[pos].most_common(1)
            if most_common:
                step = most_common[0][0]
                if step not in order:
                    order.append(step)

        return order or ["check_source", "check_tests", "check_callers"]

    def _extract_co_edit_sets(
        self, successful: list[TrajectoryRecord]
    ) -> list[list[str]]:
        """Extract files commonly edited together."""
        # Count file pairs that appear together
        pair_counts: Counter = Counter()
        for traj in successful:
            files = sorted(set(traj.files_edited))
            for i in range(len(files)):
                for j in range(i + 1, len(files)):
                    pair_counts[(files[i], files[j])] += 1

        # Return pairs that appear in ≥50% of trajectories
        threshold = len(successful) / 2
        co_edits: list[list[str]] = []
        for (f1, f2), count in pair_counts.most_common(5):
            if count >= threshold:
                co_edits.append([f1, f2])

        return co_edits

    def _extract_anti_patterns(
        self, failed: list[TrajectoryRecord]
    ) -> list[str]:
        """Extract common patterns from failed trajectories."""
        if not failed:
            return []

        # Look for files that were edited in failed but not in successful
        edit_counts: Counter = Counter()
        for traj in failed:
            for f in traj.files_edited:
                edit_counts[f] += 1

        # Files edited in >50% of failures are potential anti-patterns
        threshold = max(2, len(failed) / 2)
        anti_patterns: list[str] = []
        for file_path, count in edit_counts.most_common(3):
            if count >= threshold:
                anti_patterns.append(
                    f"Avoid editing {file_path} (led to failure in {count}/{len(failed)} cases)"
                )

        return anti_patterns

    def _extract_validation_plan(
        self, successful: list[TrajectoryRecord]
    ) -> list[str]:
        """Extract common validation steps."""
        test_counts: Counter = Counter()
        for traj in successful:
            for test in traj.tests_run:
                test_counts[test] += 1

        # Tests run in ≥50% of successful trajectories
        threshold = len(successful) / 2
        plan: list[str] = []
        for test, count in test_counts.most_common(5):
            if count >= threshold:
                plan.append(f"run {test}")

        return plan or ["run affected tests"]

    def _classify_file(self, file_path: str) -> str:
        """Classify a file by its role."""
        lower = file_path.lower()
        if "test" in lower:
            return "check_tests"
        if "__init__" in lower or "registry" in lower:
            return "check_registry"
        if "config" in lower or ".env" in lower:
            return "check_config"
        return "check_source"

    def _persist_procedure(
        self,
        signature: str,
        procedure_name: str,
        inspection_order: list[str],
        co_edit_sets: list[list[str]],
        anti_patterns: list[str],
        validation_plan: list[str],
        confidence: float,
        source_count: int,
        examples: list[TrajectoryRecord],
    ) -> None:
        """Write procedure to database."""
        steps_json = json.dumps({
            "inspection_order": inspection_order,
            "co_edit_sets": co_edit_sets,
        })
        anti_patterns_json = json.dumps(anti_patterns)
        validation_plan_json = json.dumps(validation_plan)

        try:
            cursor = self._conn.execute(
                """INSERT OR REPLACE INTO repair_procedures
                   (issue_signature, procedure_name, steps_json,
                    anti_patterns_json, validation_plan_json,
                    confidence, source_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signature,
                    procedure_name,
                    steps_json,
                    anti_patterns_json,
                    validation_plan_json,
                    confidence,
                    source_count,
                    int(time.time()),
                ),
            )
            proc_id = cursor.lastrowid

            # Insert examples
            for traj in examples[:10]:  # Cap examples
                self._conn.execute(
                    """INSERT INTO procedure_examples
                       (procedure_id, task_ref, repo_ref, outcome)
                       VALUES (?, ?, ?, ?)""",
                    (proc_id, traj.task_ref, traj.repo_ref, traj.outcome),
                )

            self._conn.commit()
        except sqlite3.Error as exc:
            logger.debug("Failed to persist procedure: %s", exc)

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        try:
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            logger.debug("Failed to create procedure schema: %s", exc)
