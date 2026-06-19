"""Dataclasses for experiment tasks, results, and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExperimentConfig(Enum):
    """Which GroundTruth configuration to run."""

    BASELINE = "baseline"
    STANDARD = "standard"
    ADAPTIVE = "adaptive"


@dataclass(frozen=True)
class ExperimentTask:
    """A single experiment task, converted from BenchmarkCase."""

    case_id: str
    category: str
    language: str
    code: str
    file_path: str
    intent: str
    correct_symbol: str
    correct_import: str


@dataclass
class ExperimentResult:
    """Per-task-per-config result."""

    case_id: str
    config: str
    category: str
    language: str

    # Validation metrics
    error_detected: bool = False
    fix_suggested: bool = False
    fix_correct: bool = False

    # Briefing metrics
    briefing_covers_correct_symbol: bool = False
    briefing_covers_correct_import: bool = False
    briefing_symbol_count: int = 0
    compliance_proxy: float = 0.0

    # Risk metrics
    file_risk_score: float = 0.0
    risk_factors: dict[str, float] = field(default_factory=dict)

    latency_ms: float = 0.0


@dataclass
class ExperimentReport:
    """Aggregate report for one config."""

    config: str
    total_tasks: int = 0
    detection_rate: float = 0.0
    fix_rate: float = 0.0
    mean_compliance_proxy: float = 0.0
    mean_risk_score: float = 0.0
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_language: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: list[ExperimentResult] = field(default_factory=list)
    elapsed_s: float = 0.0
