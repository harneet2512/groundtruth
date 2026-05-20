"""Issue-text grounding for evidence ranking.

Deterministic, $0 AI. Matches issue terms against evidence items so the
most issue-relevant evidence is shown first and irrelevant evidence is
suppressed.

Used by L3 post-edit (generate_improved_evidence) to rank collected
evidence items before delivery.
"""

from __future__ import annotations

import os
import re


_ISSUE_TERMS_PATH = "/tmp/gt_issue_terms.txt"

_STOP_WORDS = frozenset({
    "the", "that", "this", "with", "from", "have", "been", "should",
    "would", "could", "when", "where", "which", "what", "there",
    "their", "then", "than", "them", "into", "also", "some", "will",
    "does", "more", "other", "about", "like", "just", "only", "very",
    "after", "before", "between", "each", "make", "made", "using",
    "used", "uses", "need", "needs", "instead", "because", "since",
    "while", "still", "even", "being", "here", "added", "fixed",
    "expected", "return", "returns", "function", "method", "class",
    "file", "test", "tests", "error", "none", "true", "false", "self",
})


def load_issue_anchors(path: str = _ISSUE_TERMS_PATH) -> list[str]:
    """Load issue terms extracted by the wrapper at task start."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            terms = [
                line.strip().lower()
                for line in fh
                if line.strip() and line.strip().lower() not in _STOP_WORDS
            ]
        return terms
    except OSError:
        return []


def extract_code_anchors(issue_text: str) -> list[str]:
    """Extract high-signal identifiers from issue text.

    Finds: backticked identifiers, snake_case/camelCase tokens, dotted
    API names (e.g., ``remote.set_url``), parameter names (``old_url=``).
    """
    anchors: list[str] = []
    for m in re.finditer(r"`([^`]{2,60})`", issue_text):
        anchors.append(m.group(1).lower())
    for m in re.finditer(r"\b([a-z_][a-z0-9_]{3,})\b", issue_text, re.IGNORECASE):
        w = m.group(1).lower()
        if w not in _STOP_WORDS:
            anchors.append(w)
    for m in re.finditer(r"([a-zA-Z_]\w+(?:\.[a-zA-Z_]\w+)+)", issue_text):
        anchors.append(m.group(1).lower())
    for m in re.finditer(r"([a-zA-Z_]\w+)\s*=", issue_text):
        anchors.append(m.group(1).lower())
    return list(dict.fromkeys(anchors))


def score_evidence_line(line: str, anchors: list[str]) -> float:
    """Score an evidence line by overlap with issue anchors.

    Returns 0.0-1.0. Higher = more issue-relevant.
    """
    if not anchors or not line:
        return 0.0
    line_lower = line.lower()
    hits = sum(1 for a in anchors if a in line_lower)
    return min(1.0, hits / max(len(anchors) * 0.15, 1.0))


def rank_evidence_blocks(
    blocks: list[dict],
    anchors: list[str],
) -> list[dict]:
    """Re-rank evidence blocks by issue relevance.

    Each block is a dict with at least ``text`` and ``source`` keys.
    Blocks with issue-term overlap are promoted. Blocks with 0 overlap
    are demoted but not removed (they may still be useful for contract
    preservation).
    """
    if not anchors:
        return blocks
    for b in blocks:
        b["_issue_score"] = score_evidence_line(b.get("text", ""), anchors)
    return sorted(blocks, key=lambda b: (-b.get("_issue_score", 0.0), b.get("_priority", 99)))
