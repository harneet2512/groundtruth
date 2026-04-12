"""Verification domain types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PatchCandidate:
    """A candidate patch to be scored."""

    task_ref: str
    """Task identifier (e.g. SWE-bench instance ID)."""

    candidate_id: str
    """Unique identifier for this candidate."""

    diff: str
    """The unified diff content."""

    changed_files: tuple[str, ...]
    """Files modified by this patch."""

    changed_symbols: tuple[str, ...]
    """Symbols modified (function/method names)."""


@dataclass(frozen=True)
class ViolationRecord:
    """A contract violation found during verification."""

    contract_id: int
    """ID of the violated contract."""

    contract_type: str
    """Type of the violated contract."""

    predicate: str
    """The predicate that was violated."""

    severity: Literal["hard", "soft"]
    """hard = veto-worthy (contract broken), soft = warning (maintainability)."""

    explanation: str
    """Human-readable explanation of the violation."""


@dataclass(frozen=True)
class VerificationResult:
    """Complete verification output for a candidate patch."""

    candidate_id: str
    """Which candidate this result is for."""

    contract_score: float
    """0.0-1.0: how well the patch respects mined contracts."""

    test_score: float
    """0.0-1.0: estimated test pass likelihood."""

    maintainability_score: float
    """0.0-1.0: structural legality and code quality."""

    overall_score: float
    """Composite score."""

    decision: Literal["accept", "reject", "abstain"]
    """Verifier decision."""

    violations: tuple[ViolationRecord, ...]
    """All violations found."""

    recommended_tests: tuple[str, ...]
    """Test files to run for targeted validation."""

    reason_codes: tuple[str, ...]
    """Machine-readable codes: 'contract_broken', 'arity_mismatch', etc."""
