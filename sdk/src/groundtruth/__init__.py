"""GroundTruth deterministic SDK (read-path only).

This standalone distribution ships as the ``groundtruth`` package on PyPI.
"""

from __future__ import annotations

from groundtruth.core import GroundTruth
from groundtruth.models import (
    AffectedSymbol,
    Behavior,
    Briefing,
    Caller,
    ContextResult,
    Impact,
    ResolutionMethod,
    Rule,
)

__all__ = [
    "GroundTruth",
    "Briefing",
    "Impact",
    "ContextResult",
    "Caller",
    "Behavior",
    "Rule",
    "AffectedSymbol",
    "ResolutionMethod",
]

__version__ = "1.0.0"
