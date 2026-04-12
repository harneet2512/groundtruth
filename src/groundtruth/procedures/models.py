"""Procedure Priors domain types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProcedureCard:
    """A structured repair procedure distilled from successful trajectories.

    These are NOT raw memories or anecdotal fixes. They are structured,
    repeated patterns with empirical support across repositories.
    """

    issue_signature: str
    """Issue class signature: e.g. 'type_error:missing_attribute',
    'import_error:circular', 'test_failure:assertion_mismatch'."""

    procedure_name: str
    """Human-readable name: e.g. 'Fix missing attribute via duck typing'."""

    inspection_order: tuple[str, ...]
    """Recommended inspection sequence: ('check callers', 'check tests', 'check imports')."""

    co_edit_sets: tuple[tuple[str, ...], ...]
    """Common files edited together in this repair pattern."""

    anti_patterns: tuple[str, ...]
    """Known wrong approaches to avoid."""

    validation_plan: tuple[str, ...]
    """Steps to validate the fix: ('run test_X', 'check import Y')."""

    confidence: float
    """0.0-1.0: based on success_rate × log(source_count)."""

    source_count: int
    """Number of trajectories this was distilled from."""

    tier: Literal["verified", "likely", "possible"]
    """
    - verified: ≥5 examples, cross-repo
    - likely: 3-4 examples
    - possible: suppressed from runtime
    """


@dataclass(frozen=True)
class AntiPattern:
    """A known wrong approach for an issue class."""

    pattern_name: str
    """e.g. 'mock_database_in_migration_test'."""

    description: str
    """What the wrong approach looks like."""

    detection_signal: str
    """How to detect if the agent is about to repeat this."""

    frequency: int
    """How many times observed across trajectories."""


@dataclass(frozen=True)
class ValidationPlan:
    """Structured validation steps for a repair pattern."""

    steps: tuple[str, ...]
    """Ordered validation steps."""

    critical_tests: tuple[str, ...]
    """Test files/functions that must pass."""

    regression_checks: tuple[str, ...]
    """Symbols/files to check for unintended breakage."""
