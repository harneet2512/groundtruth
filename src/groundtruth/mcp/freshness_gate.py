"""Freshness Gate — enforces graph freshness as a precondition for structural truth.

Principle: gt-index is the source of truth for repository intelligence.
If the graph is stale, GT must suppress or abstain rather than serve
wrong callers, wrong siblings, wrong obligations.

Policy:
- FRESH: serve normal GT output (full confidence)
- SLIGHTLY_STALE: downgrade structural evidence to 'likely' (non-directive)
- STALE: abstain from graph-derived assertions entirely

This gate wraps every MCP tool response before it reaches the agent.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FreshnessVerdict:
    """Result of a freshness check for a tool call."""

    is_fresh: bool
    """True if graph is fresh enough to serve full-confidence output."""

    should_suppress: bool
    """True if structural evidence should be suppressed entirely."""

    should_downgrade: bool
    """True if evidence should be downgraded (non-directive only)."""

    reason: str
    """Human-readable explanation for telemetry."""

    stale_files: list[str]
    """Files that are stale (if any)."""

    graph_age_seconds: float | None
    """Age of graph.db in seconds (mtime vs now)."""


class FreshnessGate:
    """Gates MCP tool output based on graph freshness.

    Usage:
        gate = FreshnessGate(db_path="/tmp/graph.db", root_path="/testbed")
        verdict = gate.check(file_path="src/foo.py")
        if verdict.should_suppress:
            return {"abstained": True, "reason": verdict.reason}
        elif verdict.should_downgrade:
            # Strip assertive language, add caveat
            result["freshness_warning"] = verdict.reason
    """

    # Thresholds (seconds)
    FRESH_MAX_AGE = 60.0       # graph.db < 1 min old = fresh
    STALE_THRESHOLD = 300.0    # graph.db > 5 min old = stale (suppress)
    FILE_STALE_THRESHOLD = 10.0  # file mtime > graph.db mtime + 10s = stale

    def __init__(self, db_path: str, root_path: str) -> None:
        self._db_path = db_path
        self._root_path = root_path
        self._graph_mtime: float | None = None
        self._refresh_graph_mtime()

    def check(self, file_path: str | None = None) -> FreshnessVerdict:
        """Check freshness for a tool call.

        Args:
            file_path: Specific file being queried (for file-level freshness).
                      If None, checks overall graph freshness only.
        """
        # Refresh graph.db mtime (it may have been re-indexed)
        self._refresh_graph_mtime()

        # Check 1: Does graph.db exist?
        if self._graph_mtime is None:
            return FreshnessVerdict(
                is_fresh=False,
                should_suppress=True,
                should_downgrade=False,
                reason="graph.db not found — no structural intelligence available",
                stale_files=[],
                graph_age_seconds=None,
            )

        # Check 2: Overall graph age
        graph_age = time.time() - self._graph_mtime
        if graph_age > self.STALE_THRESHOLD:
            return FreshnessVerdict(
                is_fresh=False,
                should_suppress=True,
                should_downgrade=False,
                reason=f"graph.db is {graph_age:.0f}s old (threshold: {self.STALE_THRESHOLD}s) — abstaining from structural assertions",
                stale_files=[],
                graph_age_seconds=graph_age,
            )

        # Check 3: File-level freshness (if specific file requested)
        stale_files: list[str] = []
        if file_path:
            abs_path = self._resolve_path(file_path)
            if abs_path and os.path.exists(abs_path):
                try:
                    file_mtime = os.path.getmtime(abs_path)
                    if file_mtime > self._graph_mtime + self.FILE_STALE_THRESHOLD:
                        stale_files.append(file_path)
                except OSError:
                    pass

        if stale_files:
            if graph_age > self.FRESH_MAX_AGE:
                # Graph is older AND file is newer → suppress
                return FreshnessVerdict(
                    is_fresh=False,
                    should_suppress=True,
                    should_downgrade=False,
                    reason=f"File {file_path} modified after graph.db — structural evidence is stale",
                    stale_files=stale_files,
                    graph_age_seconds=graph_age,
                )
            else:
                # Graph is recent but file is slightly newer → downgrade
                return FreshnessVerdict(
                    is_fresh=False,
                    should_suppress=False,
                    should_downgrade=True,
                    reason=f"File {file_path} may have been modified since last index — structural evidence may be outdated",
                    stale_files=stale_files,
                    graph_age_seconds=graph_age,
                )

        # Fresh: graph is recent and file hasn't changed since indexing
        return FreshnessVerdict(
            is_fresh=True,
            should_suppress=False,
            should_downgrade=False,
            reason="fresh",
            stale_files=[],
            graph_age_seconds=graph_age,
        )

    def check_bulk(self, file_paths: list[str]) -> FreshnessVerdict:
        """Check freshness for multiple files. Returns worst-case verdict."""
        if not file_paths:
            return self.check(None)

        worst = self.check(None)  # Start with overall check
        if worst.should_suppress:
            return worst

        all_stale: list[str] = []
        for fp in file_paths:
            v = self.check(fp)
            if v.should_suppress:
                return v  # Any suppress → overall suppress
            if v.stale_files:
                all_stale.extend(v.stale_files)

        if all_stale:
            return FreshnessVerdict(
                is_fresh=False,
                should_suppress=False,
                should_downgrade=True,
                reason=f"{len(all_stale)} file(s) modified since index: {', '.join(all_stale[:3])}",
                stale_files=all_stale,
                graph_age_seconds=worst.graph_age_seconds,
            )

        return worst

    def gate_response(self, result: dict[str, Any], file_path: str | None = None) -> dict[str, Any]:
        """Apply freshness gate to a tool response.

        Modifies the response in-place:
        - If suppress: replaces with abstention message
        - If downgrade: adds freshness warning caveat
        - Always adds freshness telemetry

        Returns the (possibly modified) response.
        """
        verdict = self.check(file_path)

        # Telemetry (always emit)
        result["_freshness"] = {
            "is_fresh": verdict.is_fresh,
            "graph_age_seconds": verdict.graph_age_seconds,
            "action": "serve" if verdict.is_fresh else ("suppress" if verdict.should_suppress else "downgrade"),
        }

        if verdict.should_suppress:
            logger.warning(
                "[GT_FRESHNESS] SUPPRESS: %s (file=%s)",
                verdict.reason, file_path,
            )
            return {
                "abstained": True,
                "reason": verdict.reason,
                "_freshness": result["_freshness"],
            }

        if verdict.should_downgrade:
            logger.info(
                "[GT_FRESHNESS] DOWNGRADE: %s (file=%s)",
                verdict.reason, file_path,
            )
            result["freshness_warning"] = verdict.reason
            # Strip assertive language from obligations
            if "obligations" in result:
                result["obligations_confidence"] = "downgraded — index may be stale"

        return result

    def _resolve_path(self, file_path: str) -> str | None:
        """Resolve a relative file path to absolute."""
        if os.path.isabs(file_path):
            return file_path
        abs_path = os.path.join(self._root_path, file_path)
        return abs_path

    def _refresh_graph_mtime(self) -> None:
        """Refresh cached graph.db mtime."""
        try:
            self._graph_mtime = os.path.getmtime(self._db_path)
        except OSError:
            self._graph_mtime = None
