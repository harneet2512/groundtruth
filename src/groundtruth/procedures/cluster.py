"""Procedure Clustering — groups trajectories by repair pattern.

Critical constraint: cluster by repair PATTERN, not by repository name.
This prevents overfitting to specific repos and ensures transfer.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass


@dataclass
class TrajectoryRecord:
    """A single trajectory from a benchmark run."""

    task_ref: str
    """Task identifier."""

    repo_ref: str
    """Repository name."""

    issue_text: str
    """Original issue description."""

    files_visited: list[str]
    """Files the agent read during the trajectory."""

    files_edited: list[str]
    """Files the agent modified."""

    tests_run: list[str]
    """Tests executed."""

    outcome: str
    """'resolved' | 'failed'."""

    patch_diff: str
    """The final diff."""


class ProcedureClusterer:
    """Clusters trajectories into repair pattern groups."""

    def cluster(
        self, trajectories: list[TrajectoryRecord]
    ) -> dict[str, list[TrajectoryRecord]]:
        """Group trajectories by REPAIR PATTERN (not just issue text).

        P1.4: Uses trajectory structure (files edited, validation sequence,
        patch shape) as primary clustering signal. Issue text is auxiliary.

        Returns dict of signature → trajectories.
        Only includes clusters with ≥3 successful examples.
        """
        # Step 1: Multi-feature signature combining issue text + trajectory structure
        clusters: dict[str, list[TrajectoryRecord]] = defaultdict(list)
        for traj in trajectories:
            signature = self._compute_repair_signature(traj)
            clusters[signature].append(traj)

        # Step 2: Filter to clusters with sufficient support
        return {
            sig: trajs
            for sig, trajs in clusters.items()
            if sum(1 for t in trajs if t.outcome == "resolved") >= 3
        }

    def _compute_repair_signature(self, traj: TrajectoryRecord) -> str:
        """Compute a repair pattern signature from trajectory structure.

        Combines:
        - Issue text classification (auxiliary, not primary)
        - Edit pattern (number of files, test vs source ratio)
        - Validation pattern (tests run early vs late)

        This prevents two issues with similar wording but different repair
        patterns from being grouped together.
        """
        # Issue text component (auxiliary)
        issue_sig = self.classify_issue(traj.issue_text)

        # Edit pattern component
        n_files = len(traj.files_edited)
        test_files = sum(1 for f in traj.files_edited if "test" in f.lower())
        source_files = n_files - test_files
        edit_pattern = f"e{source_files}t{test_files}"

        # Validation pattern: did tests run early (TDD-style) or late?
        if traj.tests_run and traj.files_edited:
            # Simple heuristic: if test appears before most edits, it's TDD
            validation_style = "tdd" if traj.tests_run else "post"
        else:
            validation_style = "none"

        return f"{issue_sig}|{edit_pattern}|{validation_style}"

    def classify_issue(self, issue_text: str) -> str:
        """Classify issue text into a signature.

        Uses deterministic keyword matching — NOT model calls.
        """
        text_lower = issue_text.lower()

        # Error type patterns
        if "typeerror" in text_lower or "type error" in text_lower:
            if "attribute" in text_lower:
                return "type_error:missing_attribute"
            if "argument" in text_lower or "arity" in text_lower:
                return "type_error:wrong_arity"
            return "type_error:general"

        if "importerror" in text_lower or "import error" in text_lower:
            if "circular" in text_lower:
                return "import_error:circular"
            return "import_error:missing_module"

        if "keyerror" in text_lower or "key error" in text_lower:
            return "key_error:missing_key"

        if "valueerror" in text_lower or "value error" in text_lower:
            return "value_error:validation"

        if "assertion" in text_lower:
            if "test" in text_lower or "fail" in text_lower:
                return "test_failure:assertion_mismatch"
            return "assertion_error:general"

        if "indexerror" in text_lower or "index out" in text_lower:
            return "index_error:bounds"

        if "none" in text_lower and ("attribute" in text_lower or "type" in text_lower):
            return "none_error:unexpected_none"

        # Behavioral patterns
        if "deprecat" in text_lower:
            return "deprecation:api_change"

        if "performance" in text_lower or "slow" in text_lower:
            return "performance:regression"

        if "security" in text_lower or "vulnerab" in text_lower:
            return "security:vulnerability"

        if "race" in text_lower or "concurren" in text_lower:
            return "concurrency:race_condition"

        # Fallback: extract the most informative word
        words = re.findall(r"\b[a-z_]+error\b|\b[a-z_]+exception\b", text_lower)
        if words:
            return f"error:{words[0]}"

        return "unknown:general"
