"""Symbol name component splitting and matching."""

from __future__ import annotations

import re

from groundtruth.utils.levenshtein import levenshtein_distance


def split_symbol_name(name: str) -> list[str]:
    """Split a symbol name into lowercase components.

    Handles snake_case, camelCase, PascalCase, SCREAMING_SNAKE, and acronyms.
    Examples:
        "getUserById"  -> ["get", "user", "by", "id"]
        "HTTPClient"   -> ["http", "client"]
        "my_func_name" -> ["my", "func", "name"]
        "XMLParser"    -> ["xml", "parser"]
    """
    if not name:
        return []

    # First split on underscores (handles snake_case and SCREAMING_SNAKE)
    parts: list[str] = []
    for segment in name.split("_"):
        if not segment:
            continue
        # Split camelCase/PascalCase:
        # Insert boundary before: lowercase→uppercase, or acronym→PascalWord
        # e.g. "getUserById" -> "get|User|By|Id"
        # e.g. "HTTPClient" -> "HTTP|Client"
        tokens = re.sub(
            r"([A-Z]+)([A-Z][a-z])",  # Acronym followed by PascalWord
            r"\1_\2",
            segment,
        )
        tokens = re.sub(
            r"([a-z\d])([A-Z])",  # lowercase/digit followed by uppercase
            r"\1_\2",
            tokens,
        )
        for token in tokens.split("_"):
            if token:
                parts.append(token.lower())

    return parts


def suggest_by_components(
    name: str,
    candidates: list[str],
    min_overlap: int = 1,
    max_results: int = 5,
) -> list[tuple[str, float]]:
    """Find candidates sharing name components, scored by overlap ratio.

    Returns list of (candidate_name, score) sorted by score desc, then levenshtein asc.
    Score = |shared_components| / max(|query_components|, |candidate_components|).
    """
    query_parts = split_symbol_name(name)
    if not query_parts:
        return []

    query_set = set(query_parts)
    scored: list[tuple[str, float, int]] = []

    for candidate in candidates:
        cand_parts = split_symbol_name(candidate)
        if not cand_parts:
            continue
        cand_set = set(cand_parts)
        shared = len(query_set & cand_set)
        if shared < min_overlap:
            continue
        score = shared / max(len(query_set), len(cand_set))
        dist = levenshtein_distance(name, candidate)
        scored.append((candidate, score, dist))

    # Sort by score descending, then levenshtein ascending
    scored.sort(key=lambda x: (-x[1], x[2]))
    return [(s[0], s[1]) for s in scored[:max_results]]
