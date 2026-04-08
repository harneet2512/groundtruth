"""Abstention policy — formalized "positive evidence only" decision table.

Every subsystem calls this to decide whether to emit a finding. The policy
encodes a simple rule: if we don't have strong enough evidence, stay silent.
Saying nothing is always safer than a false positive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EmissionLevel(Enum):
    """What kind of finding to emit."""

    EMIT_NOTHING = "emit_nothing"
    EMIT_SOFT_INFO = "emit_soft_info"
    EMIT_HARD_BLOCKER = "emit_hard_blocker"


class TrustTier(Enum):
    """How much we trust the underlying evidence."""

    GREEN = "green"  # runtime-confirmed or high-evidence
    YELLOW = "yellow"  # AST-only or partial coverage
    RED = "red"  # unknown or missing data


# Minimum pieces of evidence before we trust a finding.
MIN_EVIDENCE_COUNT = 2

# Minimum known symbols in a module before we trust the index.
# Matches _MIN_COVERAGE_THRESHOLD in ast_validator.py.
MIN_COVERAGE_THRESHOLD = 5


@dataclass(frozen=True)
class AbstentionPolicy:
    """Lookup-table policy for emission decisions.

    Parameters allow overriding thresholds for testing or per-subsystem tuning.
    """

    min_evidence: int = MIN_EVIDENCE_COUNT
    min_coverage: float = MIN_COVERAGE_THRESHOLD

    def decide(
        self,
        trust: TrustTier,
        evidence_count: int,
        coverage: float,
        is_stale: bool = False,
        is_contradiction: bool = True,
    ) -> EmissionLevel:
        """Decide what to emit given the current evidence state.

        Args:
            trust: How reliable the underlying data source is.
            evidence_count: Number of concrete evidence items supporting this finding.
            coverage: Number of known symbols in the relevant module/scope.
            is_stale: Whether the index is out-of-date.
            is_contradiction: Whether the finding is a hard contradiction (vs. obligation/info).

        Returns:
            The appropriate emission level.
        """
        # RED trust: never emit, regardless of other signals.
        if trust == TrustTier.RED:
            return EmissionLevel.EMIT_NOTHING

        # YELLOW trust: emit only with fresh index and enough evidence.
        if trust == TrustTier.YELLOW:
            if is_stale:
                return EmissionLevel.EMIT_NOTHING
            if evidence_count < self.min_evidence:
                return EmissionLevel.EMIT_NOTHING
            if coverage < self.min_coverage:
                return EmissionLevel.EMIT_NOTHING
            return EmissionLevel.EMIT_SOFT_INFO

        # GREEN trust: contradictions are hard blockers, everything else is soft.
        if trust == TrustTier.GREEN:
            if is_contradiction:
                return EmissionLevel.EMIT_HARD_BLOCKER
            return EmissionLevel.EMIT_SOFT_INFO

        return EmissionLevel.EMIT_NOTHING  # unreachable, but defensive

    def should_emit(
        self,
        trust: TrustTier,
        evidence_count: int,
        coverage: float,
        is_stale: bool = False,
    ) -> bool:
        """Convenience: returns True if the policy would emit anything at all."""
        return self.decide(trust, evidence_count, coverage, is_stale) != EmissionLevel.EMIT_NOTHING
