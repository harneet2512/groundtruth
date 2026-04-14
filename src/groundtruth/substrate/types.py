"""Core domain types for the GT substrate layer.

All types are frozen dataclasses: immutable, hashable, and safe for
concurrent use. Sequences use tuple rather than list to preserve the
frozen contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Confidence model
# ---------------------------------------------------------------------------

ConfidenceTier = Literal["verified", "likely", "possible"]
FreshnessState = Literal["fresh", "stale", "unknown"]
SupportKind = Literal[
    "tests",
    "callers",
    "siblings_or_pairs",
    "docs_or_config",
    "runtime_or_exec",
    "structure",
]

MIN_LIKELY_CONFIDENCE = 0.70
MIN_VERIFIED_CONFIDENCE = 0.85
MIN_VERIFIED_SUPPORT = 2


def tier_from_confidence(confidence: float, support_count: int = 1) -> ConfidenceTier:
    """Derive a tier from numeric confidence and support count.

    DEPRECATED: Legacy promotion model. Used only by non-vNext extractors.
    vNext families must use promote_tier() from groundtruth.substrate.promotion.
    Legacy contracts produced by this function are isolated from vNext semantic
    paths (semantic constraints, patch scoring, hard rejection).
    """
    if support_count >= MIN_VERIFIED_SUPPORT and confidence >= MIN_VERIFIED_CONFIDENCE:
        return "verified"
    if confidence >= MIN_LIKELY_CONFIDENCE:
        return "likely"
    return "possible"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceItem:
    """Single piece of ranked evidence."""

    family: str
    score: int
    name: str
    file: str
    line: int
    source_code: str
    summary: str
    confidence: float
    tier: ConfidenceTier


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractRecord:
    """A behavioral contract extracted from repository evidence."""

    contract_type: str
    scope_kind: str
    scope_ref: str
    predicate: str
    normalized_form: str
    support_sources: tuple[str, ...]
    support_count: int
    confidence: float
    tier: ConfidenceTier
    support_kinds: tuple[SupportKind, ...] = ()
    scope_file: str | None = None
    checkable: bool = True
    freshness_state: FreshnessState = "unknown"


@dataclass(frozen=True)
class ContractBundle:
    """Contracts applicable to a set of scopes and changed files."""

    contracts: tuple[ContractRecord, ...]
    scope_refs: tuple[str, ...]
    changed_files: tuple[str, ...]
    freshness_state: FreshnessState


# ---------------------------------------------------------------------------
# Localization / repository intelligence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalizationTarget:
    """A candidate symbol for the agent to investigate."""

    node_id: int
    name: str
    file_path: str
    start_line: int
    confidence: float
    tier: ConfidenceTier
    file_confidence: float
    symbol_confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LocalizationResult:
    """Complete localization output for an issue."""

    candidates: tuple[LocalizationTarget, ...]
    structural_unlocked: bool
    issue_identifiers: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of canonical symbol resolution."""

    symbol: str
    status: Literal["resolved", "missing", "ambiguous"]
    matches: tuple[tuple[str, str], ...]
    resolved_node_id: int | None = None
    resolved_file: str | None = None
    qualified_name: str | None = None


@dataclass(frozen=True)
class ConstraintHint:
    """A single 'must remain true' constraint for the pre-edit brief.

    Kept short and hard-edged: text should be one sentence, machine-derivable,
    and actionable by the agent before making an edit.
    """

    text: str
    tier: ConfidenceTier
    family: str
    checkable: bool
    support_summary: str


@dataclass(frozen=True)
class RepoIntelBrief:
    """Typed startup brief for sparse runtime delivery."""

    top_candidate_file: str | None
    backup_files: tuple[str, ...]
    candidate_symbols: tuple[str, ...]
    likely_bug_mechanism: str
    do_not_break: tuple[str, ...]
    repro_hint: str | None
    confidence: Literal["high", "medium", "broad_search"]
    issue_identifiers: tuple[str, ...]
    semantic_constraints: tuple[ConstraintHint, ...] = ()


# ---------------------------------------------------------------------------
# Patch scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchScore:
    """Result of scoring a candidate patch against contracts and tests."""

    candidate_id: str
    contract_score: float
    test_score: float
    maintainability_score: float
    overall_score: float
    decision: Literal["accept", "reject", "abstain"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PatchVerdict:
    """Canonical patch-scoring result exposed by the substrate service."""

    decision: Literal["accept", "reject", "abstain"]
    overall_score: float
    contract_score: float
    test_score: float
    maintainability_score: float
    hard_violations: tuple[str, ...]
    soft_warnings: tuple[str, ...]
    abstentions: tuple[str, ...]
    reason_codes: tuple[str, ...]
    recommended_next_check: str | None
