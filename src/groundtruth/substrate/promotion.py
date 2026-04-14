"""Evidence-diversity-based contract promotion.

Replaces the naive confidence+support_count model with a weighted evidence
class system. A contract earns higher tiers by drawing on multiple independent
evidence classes, not by accumulating many examples from the same class.

Evidence class weights
----------------------
tests            3   -- strongest: expresses intended contract directly
runtime_or_exec  3   -- strongest: observed at execution time
callers          2   -- strong: usage-driven contract derivation
siblings_or_pairs 2  -- strong: structural / inverse-pair evidence
structure        1   -- weak: naming, typing, graph relationships
docs_or_config   1   -- weak: documentation and config signals

Promotion rules
---------------
verified  weighted_score >= 4  AND  any strong class present
          (tests, runtime_or_exec, or callers must be in support_kinds)
likely    weighted_score >= 2
possible  otherwise
"""

from __future__ import annotations

from typing import Iterable

from groundtruth.substrate.types import ConfidenceTier, SupportKind

# ---------------------------------------------------------------------------
# Weight table
# ---------------------------------------------------------------------------

_WEIGHTS: dict[SupportKind, int] = {
    "tests": 3,
    "runtime_or_exec": 3,
    "callers": 2,
    "siblings_or_pairs": 2,
    "structure": 1,
    "docs_or_config": 1,
}

_STRONG_CLASSES: frozenset[SupportKind] = frozenset(
    {"tests", "runtime_or_exec", "callers"}
)

# Thresholds
_VERIFIED_THRESHOLD = 4
_LIKELY_THRESHOLD = 2


def promote_tier(support_kinds: Iterable[str]) -> ConfidenceTier:
    """Derive a confidence tier from a collection of evidence class labels.

    Parameters
    ----------
    support_kinds:
        The distinct evidence classes contributing to a contract.
        Duplicates are ignored — only distinct classes are counted.

    Returns
    -------
    "verified", "likely", or "possible".

    Examples
    --------
    >>> promote_tier(["tests", "callers"])
    'verified'
    >>> promote_tier(["callers", "callers", "callers"])
    'likely'
    >>> promote_tier(["structure"])
    'possible'
    >>> promote_tier(["siblings_or_pairs", "structure"])
    'likely'
    """
    distinct: frozenset[SupportKind] = frozenset(support_kinds)  # type: ignore[arg-type]
    score = sum(_WEIGHTS.get(k, 0) for k in distinct)
    has_strong = bool(distinct & _STRONG_CLASSES)

    if score >= _VERIFIED_THRESHOLD and has_strong:
        return "verified"
    if score >= _LIKELY_THRESHOLD:
        return "likely"
    return "possible"


def weighted_score(support_kinds: Iterable[str]) -> int:
    """Return the raw weighted score for a set of evidence classes.

    Useful for ranking contracts within the same tier.
    """
    distinct: frozenset[SupportKind] = frozenset(support_kinds)  # type: ignore[arg-type]
    return sum(_WEIGHTS.get(k, 0) for k in distinct)
