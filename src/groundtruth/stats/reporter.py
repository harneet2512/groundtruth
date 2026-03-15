"""Generates stats summaries from intervention data."""

from __future__ import annotations

from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


class StatsReporter:
    """Generates human-readable stats reports."""

    def __init__(self, tracker: InterventionTracker) -> None:
        self._tracker = tracker

    def generate_report(self) -> Result[str, GroundTruthError]:
        """Generate a formatted stats report."""
        result = self._tracker.get_stats()
        if isinstance(result, Err):
            return result

        stats = result.value
        lines = [
            "GroundTruth Intervention Report",
            "=" * 35,
            f"Total interventions:      {stats.total}",
            f"Hallucinations caught:    {stats.hallucinations_caught}",
            f"AI calls:                 {stats.ai_calls}",
            f"Tokens used:              {stats.tokens_used}",
        ]
        return Ok("\n".join(lines))
