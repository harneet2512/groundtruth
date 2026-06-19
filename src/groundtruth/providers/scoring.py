"""Issue-relevance scoring helpers (pure)."""

from __future__ import annotations

import os
from typing import Iterable


def issue_relevance_scorer(text: str, issue_terms: Iterable[str]) -> float:
    """Fraction of issue terms appearing in ``text`` (case-insensitive).

    Returns 0.0 when ``issue_terms`` is empty (caller decides neutral vs zero).
    """
    terms = [t for t in issue_terms if t]
    if not terms:
        return 0.0
    haystack = text.lower()
    hits = sum(1 for t in terms if t.lower() in haystack)
    return hits / len(terms)


def score_edges_by_issue_relevance(
    edges: list[tuple[str, int]],
    repo_root: str,
    issue_terms: Iterable[str],
    *,
    max_chars: int = 200_000,
) -> list[tuple[str, int, int]]:
    """Re-rank ``[(file_path, count)]`` edges by issue keyword hits in the file body.

    Returns ``[(file_path, count, hits)]`` sorted by ``hits`` descending. Files
    that can't be opened contribute ``hits=0`` (still appear, just unranked by
    relevance).
    """
    terms = [t.lower() for t in issue_terms if t]
    if not terms:
        return [(fp, cnt, 0) for fp, cnt in edges]
    scored: list[tuple[str, int, int]] = []
    for fp, cnt in edges:
        try:
            with open(os.path.join(repo_root, fp), encoding="utf-8", errors="ignore") as fh:
                body = fh.read(max_chars).lower()
            hits = sum(1 for t in terms if t in body)
        except OSError:
            hits = 0
        scored.append((fp, cnt, hits))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


__all__ = ["issue_relevance_scorer", "score_edges_by_issue_relevance"]
