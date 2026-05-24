"""Enforce per-task budgets for GT signals and tools.

Budgets prevent flooding. Each layer/tool has a maximum number of
fires per task. Over-budget attempts are suppressed with reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_BUDGETS: dict[str, int] = {
    "L3": 5,
    "L3b": 3,
    "L4_auto": 2,
    "L5": 2,
    "gt_query": 3,
    "gt_search": 3,
    "gt_navigate": 2,
    "gt_validate": 2,
}


@dataclass
class BudgetTracker:
    """Per-task budget enforcement."""
    limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    counts: dict[str, int] = field(default_factory=dict)

    def check(self, key: str) -> tuple[bool, str]:
        """Returns (allowed, reason). Increments count if allowed."""
        limit = self.limits.get(key, 999)
        current = self.counts.get(key, 0)
        if current >= limit:
            return False, f"budget_exhausted:{key}={current}/{limit}"
        self.counts[key] = current + 1
        return True, ""

    def remaining(self, key: str) -> int:
        limit = self.limits.get(key, 999)
        current = self.counts.get(key, 0)
        return max(0, limit - current)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": dict(self.limits),
            "counts": dict(self.counts),
            "remaining": {k: self.remaining(k) for k in self.limits},
        }
