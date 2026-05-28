"""Orientation composite scoring — dynamic + hybrid + confidence-gated.

Per `.claude/CLAUDE.md`: every layer fix must satisfy all three properties.

This module computes per-candidate composite scores from 5 signals and
derives confidence tiers from per-task score distribution. The wrapper
uses this for L1+ orientation rendering (DOC_OF_HONOR §2.1+).

Research basis:
- LocAgent ACL 2025: top-N candidate ranking; multi-feature scoring
- SweRank ICLR 2025: identifier part splitting (snake_case / camelCase)
- CodePlan FSE 2024: inverse-frequency penalizes structural hubs
- TF-IDF (Salton 1971): high-frequency = low information content
- PyCG ICSE 2021: structural property evidence for relevance

Weights are explicit and traceable to research above.
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Iterable

# Weights sum to 1.15 (5 signals, all additive). The "property" weight is
# additive (not a separate bonus accumulator). A perfect-signal candidate
# scores at most 1.15.
_W_DIRECT_MATCH = 0.40   # LocAgent ACL 2025: direct name match dominates
_W_PART_OVERLAP = 0.25   # SweRank ICLR 2025: subword overlap
_W_PATH_OVERLAP = 0.15   # LocAgent: file path heuristic
_W_INVERSE_HUB = 0.20    # CodePlan FSE 2024 / TF-IDF
_W_PROP_MATCH = 0.15     # PyCG-style structural evidence

_CLASS_CONTEXT_DEMOTE = 0.4   # Classes named in issue text usually context, not target

_PART_SPLIT_RE = re.compile(r"[_]|(?<=[a-z])(?=[A-Z])")
_PATH_SPLIT_RE = re.compile(r"[_/\.\-]")

_COMMON_PARTS = frozenset({
    "get", "set", "is", "has", "to", "from", "of", "in", "on", "at",
    "by", "with", "for", "and", "or", "not", "the", "a", "an",
    "self", "cls", "obj", "args", "kwargs", "data", "value", "item",
})

_COMMON_PATH = frozenset({
    # extension-/scaffold-like tokens; generalized across languages
    "src", "lib", "test", "tests", "spec", "specs",
    "py", "js", "ts", "tsx", "jsx", "go", "rs", "rb", "java", "kt",
    "c", "h", "cpp", "hpp", "cc", "cxx", "m", "mm",
    "php", "swift", "scala", "clj", "ex", "exs", "erl",
    "core", "utils", "util", "helpers", "common", "internal", "pkg",
    "main", "app", "config", "include", "vendor", "public", "private",
    "module", "modules", "package", "packages", "components",
    "", "groundtruth",
})

# Class-like declaration labels across languages emitted by gt-index.
_CLASS_LABELS = frozenset({
    "Class", "Interface", "Struct",
    "Trait", "Enum", "Type", "Module", "Object", "Protocol", "Mixin",
})


def _direct_name_match(name: str, issue_text: str) -> float:
    """Signal 1: function name appears literally in issue text. 0 or 1."""
    if not name or not issue_text:
        return 0.0
    return 1.0 if name.lower() in issue_text.lower() else 0.0


def _part_overlap(name: str, issue_kws: Iterable[str]) -> float:
    """Signal 2: snake_case / camelCase parts overlap issue keywords.

    Normalized to [0, 1] by dividing intersection size by candidate part count
    (so short generic names like 'get' don't get artificially high scores
    from single-keyword issues).
    """
    if not name or not issue_kws:
        return 0.0
    parts = set(_PART_SPLIT_RE.split(name))
    parts = {p.lower() for p in parts if p and p.lower() not in _COMMON_PARTS}
    if not parts:
        return 0.0
    kws_lower = {k.lower() for k in issue_kws}
    overlap = len(parts & kws_lower)
    return min(1.0, overlap / max(1, len(parts)))


def _path_overlap(file_path: str, issue_kws: Iterable[str]) -> float:
    """Signal 3: file path tokens overlap issue keywords."""
    if not file_path or not issue_kws:
        return 0.0
    tokens = set(_PATH_SPLIT_RE.split(file_path.lower()))
    tokens = {t for t in tokens if t and t not in _COMMON_PATH}
    if not tokens:
        return 0.0
    kws_lower = {k.lower() for k in issue_kws}
    overlap = len(tokens & kws_lower)
    return min(1.0, overlap / max(1, len(tokens)))


def _inverse_hub_score(caller_count: int) -> float:
    """Signal 4: penalize hubs (high caller-count = low information content).

    Maps caller_count → [0, 1]:
      0 → 1.0    (leaf, very specific)
      1 → 0.59
      5 → 0.36
      20 → 0.25
      100 → 0.18
    """
    if caller_count < 0:
        caller_count = 0
    return 1.0 / (1.0 + math.log(1.0 + caller_count))


def _property_evidence_match(
    properties: list[dict] | None,
    issue_text: str,
    issue_kws: Iterable[str],
) -> float:
    """Signal 5: function's guard_clause/raise/conditional text overlaps issue.

    Looks for issue keywords (4+ chars) in property values. 0 or 1.
    """
    if not properties or not issue_text:
        return 0.0
    kws = [k for k in issue_kws if len(k) >= 4]
    if not kws:
        return 0.0
    for prop in properties:
        val = (prop.get("value") or "")[:200].lower()
        if not val:
            continue
        for kw in kws:
            if kw.lower() in val:
                return 1.0
    return 0.0


def composite_score(
    *,
    name: str,
    label: str,
    file_path: str,
    caller_count: int,
    properties: list[dict] | None,
    issue_text: str,
    issue_kws: set[str],
) -> tuple[float, dict[str, float]]:
    """Compute hybrid composite from 5 signals with research-cited weights.

    Returns (score, signals) where signals is a per-signal breakdown for
    telemetry. Score is in [0, 1+] (bonus signal can push above 1.0).
    """
    direct = _direct_name_match(name, issue_text)
    part = _part_overlap(name, issue_kws)
    path = _path_overlap(file_path, issue_kws)
    inv_hub = _inverse_hub_score(caller_count)
    prop = _property_evidence_match(properties, issue_text, issue_kws)

    score = (
        _W_DIRECT_MATCH * direct
        + _W_PART_OVERLAP * part
        + _W_PATH_OVERLAP * path
        + _W_INVERSE_HUB * inv_hub
        + _W_PROP_MATCH * prop
    )

    is_class = label in _CLASS_LABELS
    if is_class and direct > 0:
        score *= _CLASS_CONTEXT_DEMOTE

    return score, {
        "direct": direct,
        "part": part,
        "path": path,
        "inverse_hub": inv_hub,
        "prop": prop,
    }


# Signal decomposition tiering — Option B per Cursor mental model.
#
# Tiers derive from WHICH categorical signals fired per candidate, NOT
# from a numeric composite total. This mirrors how Cursor and LSP-based
# harnesses work: "go to definition" (verified), "find references"
# (matched), "regex fallback" (fuzzy) are discrete categories of evidence.
# The agent reads the category and knows what the evidence type is —
# no opaque "73% confidence" scores.
#
# Categorical signal classes:
#   - "dominant": direct name match, majority part overlap, property match
#     -> these are strong categorical claims about relevance
#   - "meaningful": partial part/path overlap, any structural anchor
#     -> there is SOME semantic connection to the issue
#   - "leaf-only": only inverse_hub fired (every leaf function gets this)
#     -> no semantic anchor, just a structural property of being non-hub
#
# Per-signal "dominance" definitions are categorical, not statistical
# thresholds. They define what counts as "this category of evidence fired."

# Categorical dominance thresholds — these define evidence categories,
# not score regimes. `part >= 0.5` means "majority of name parts overlap
# the issue" — a categorical claim about the evidence type.
_PART_DOMINANT_FRACTION = 0.5  # majority of identifier parts match


def _tier_from_signals(signals: dict[str, float]) -> str:
    """Per-candidate tier from categorical signal contributions.

    Maps the signal dict produced by ``composite_score()`` to a confidence
    tier based on which evidence categories fired. No composite-total
    thresholds, no statistical regime selectors.

    [VERIFIED] — at least one dominant categorical signal fired:
        direct == 1.0       (function name appears in issue text)
        part >= 0.5         (majority of identifier parts overlap issue)
        prop == 1.0         (guard/raise property text matches issue keyword)

    [WARNING] — at least one meaningful structural signal fired:
        any of direct/part/path/prop > 0 (any semantic anchor present)

    [INFO] — only universal inverse_hub accumulated (every leaf gets this).
        No semantic anchor to the issue at all.
    """
    direct = signals.get("direct", 0.0)
    part = signals.get("part", 0.0)
    path = signals.get("path", 0.0)
    prop = signals.get("prop", 0.0)

    if direct >= 1.0 or part >= _PART_DOMINANT_FRACTION or prop >= 1.0:
        return "[VERIFIED]"
    if direct > 0.0 or part > 0.0 or path > 0.0 or prop > 0.0:
        return "[WARNING]"
    return "[INFO]"


def signal_decomposition_tiers(signals_list: list[dict[str, float]]) -> list[str]:
    """Per-candidate tier list from signal dicts.

    Returns tier strings matching input order. Each tier is independent
    (no cross-candidate normalization or relative ranking).
    """
    return [_tier_from_signals(s) for s in signals_list]


# Backward-compatible alias for existing call sites that used the old
# score-based name. Wrapper will be migrated to signal_decomposition_tiers.
def dynamic_tiers(signals_or_scores) -> list[str]:
    """Compatibility shim.

    If the argument is a list of dicts, treats them as signal dicts and
    returns signal-decomposition tiers. If passed floats (legacy), maps
    every candidate to [INFO] — legacy callers must migrate.
    """
    if not signals_or_scores:
        return []
    first = signals_or_scores[0]
    if isinstance(first, dict):
        return signal_decomposition_tiers(signals_or_scores)
    return ["[INFO]"] * len(signals_or_scores)


def render_orientation(
    candidates: list[dict],
    tiers: list[str],
    *,
    max_per_section: int = 3,
) -> tuple[list[str], dict[str, int]]:
    """Render confidence-gated orientation lines.

    Returns (lines, counts) where counts is per-tier breakdown for telemetry.

    [VERIFIED] candidates → "Issue references:" section
    [WARNING]  candidates → "Related (by graph):" section
    [INFO]     candidates → suppressed
    No VERIFIED+WARNING → honest fallback note
    """
    verified: list[dict] = []
    warning: list[dict] = []
    info_count = 0
    for cand, tier in zip(candidates, tiers):
        if tier == "[VERIFIED]":
            verified.append(cand)
        elif tier == "[WARNING]":
            warning.append(cand)
        else:
            info_count += 1

    lines: list[str] = []
    if verified:
        lines.append("Issue references:")
        for c in verified[:max_per_section]:
            lines.append(_format_candidate_line(c))
    if warning:
        lines.append("Related (by graph):")
        for c in warning[:max_per_section]:
            lines.append(_format_candidate_line(c))

    if not lines:
        lines.append(
            "Note: GT could not match function names to issue text with "
            "sufficient confidence. Use grep on issue keywords to localize."
        )

    counts = {
        "verified": len(verified),
        "warning": len(warning),
        "info_suppressed": info_count,
    }
    return lines, counts


def _format_candidate_line(c: dict) -> str:
    """Format a single candidate line for orientation output."""
    import os
    name = c.get("func", "?")
    file_path = c.get("file", "")
    is_class = c.get("label") in _CLASS_LABELS or c.get("is_class")
    callers = int(c.get("callers", 0) or 0)
    tag = " [class]" if is_class else ""
    caller_str = f" ({callers} callers)" if callers > 0 else ""
    base = os.path.basename(file_path) if file_path else "?"
    return f"  {name}() in {base}{tag}{caller_str}"
