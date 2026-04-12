"""Core domain types for the GT substrate layer.

All types are frozen dataclasses — immutable, hashable, safe for
concurrent use. Sequences use tuple (not list) to preserve frozen
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Confidence model
# ---------------------------------------------------------------------------

ConfidenceTier = Literal["verified", "likely", "possible"]
"""
- verified: ≥2 independent sources, assertive guidance permitted
- likely: 1 strong source, non-directive shortlist only
- possible: suppressed from runtime output (logging/debug only)
"""


def tier_from_confidence(confidence: float, support_count: int = 1) -> ConfidenceTier:
    """Derive tier from numeric confidence + support count.

    Rules (from engineering plan §7):
    - verified: support_count >= 2 AND confidence >= 0.85
    - likely: confidence >= 0.6 (or single strong source >= 0.8)
    - possible: everything else
    """
    if support_count >= 2 and confidence >= 0.85:
        return "verified"
    if confidence >= 0.6:
        return "likely"
    return "possible"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceItem:
    """Single piece of ranked evidence.

    Replaces the ad-hoc EvidenceNode from gt_intel.py with a typed,
    confidence-gated representation.
    """

    family: str
    """Evidence family: CALLER, SIBLING, TEST, IMPACT, TYPE, PRECEDENT,
    OBLIGATION, IMPORT, NEGATIVE, CRITIQUE."""

    score: int
    """Relevance score 0-3 (higher = more important)."""

    name: str
    """Symbol name this evidence concerns."""

    file: str
    """Source file path."""

    line: int
    """Source line number."""

    source_code: str
    """Actual code snippet or import statement."""

    summary: str
    """Human-readable one-line summary."""

    confidence: float
    """Numeric confidence 0.0-1.0."""

    tier: ConfidenceTier
    """Derived confidence tier."""


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractRecord:
    """A behavioral contract extracted from code evidence.

    Contracts are the bridge between localization (where to look) and
    correctness (what must remain true). They are deterministic,
    confidence-gated, and machine-checkable where possible.
    """

    contract_type: str
    """Contract family: exception_message | exact_output | roundtrip |
    type_shape | registry_coupling | symmetry_inverse."""

    scope_kind: str
    """Scope level: function | method | class | module."""

    scope_ref: str
    """Qualified reference: e.g. 'mymod.MyClass.my_method'."""

    predicate: str
    """Human-readable: 'raises ValueError(\"x must be positive\")'."""

    normalized_form: str
    """Machine-comparable: 'raises:ValueError:x must be positive'."""

    support_sources: tuple[str, ...]
    """Provenance: ('test_foo.py:12', 'caller_bar.py:45')."""

    support_count: int
    """Number of independent supporting sources."""

    confidence: float
    """Numeric confidence 0.0-1.0."""

    tier: ConfidenceTier
    """Derived confidence tier."""


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalizationTarget:
    """A candidate symbol for the agent to investigate.

    Replaces LocalizationCandidate from gt_intel.py with frozen,
    hashable representation.
    """

    node_id: int
    """Node ID in graph.db."""

    name: str
    """Symbol name."""

    file_path: str
    """File containing this symbol."""

    start_line: int
    """Start line of the symbol definition."""

    confidence: float
    """Overall confidence 0.0-1.0."""

    tier: ConfidenceTier
    """Derived confidence tier."""

    file_confidence: float
    """Confidence contribution from file-level signals."""

    symbol_confidence: float
    """Confidence contribution from symbol-level signals."""

    reasons: tuple[str, ...]
    """Why this target was selected: ('name_match', 'file_mentioned', 'stack_trace')."""


@dataclass(frozen=True)
class LocalizationResult:
    """Complete localization output for an issue.

    Replaces LocalizationState from gt_intel.py.
    """

    candidates: tuple[LocalizationTarget, ...]
    """Ranked localization candidates."""

    structural_unlocked: bool
    """True if top candidate is 'verified' tier — enables assertive guidance."""

    issue_identifiers: tuple[str, ...]
    """Identifiers extracted from the issue text."""


# ---------------------------------------------------------------------------
# Patch scoring (Phase 2 types, defined here for interface stability)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PatchScore:
    """Result of scoring a candidate patch against contracts and tests."""

    candidate_id: str
    """Unique identifier for this candidate."""

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

    reasons: tuple[str, ...]
    """Machine-readable reason codes for the decision."""
