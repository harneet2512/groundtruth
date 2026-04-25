"""Deterministic novelty filter for finding dedup.

Simple fingerprint-based dedup — NOT the embedding-based system in
groundtruth.memory. Used per-session in MCP server, per-container in
hook harness.
"""

from __future__ import annotations

import time


from groundtruth.schema.finding import Finding


class NoveltyFilter:
    """Session-scoped finding dedup by structural identity."""

    def __init__(self) -> None:
        self._shown: dict[str, float] = {}

    def _fingerprint(self, f: Finding) -> str:
        return f"{f.kind.value}|{f.location.file}|{f.location.line}|{f.location.symbol}"

    def filter(self, findings: list[Finding]) -> list[Finding]:
        """Mark novelty=False for already-shown findings. Returns all findings."""
        result: list[Finding] = []
        for f in findings:
            fp = self._fingerprint(f)
            if fp in self._shown:
                f = f.model_copy(update={"novelty": False})
            else:
                f = f.model_copy(update={"novelty": True})
                self._shown[fp] = time.time()
            result.append(f)
        return result

    def shown_count(self) -> int:
        return len(self._shown)

    def reset(self) -> None:
        self._shown.clear()
