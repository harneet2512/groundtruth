"""v2.2 — path-segment BM25 scoring.

Tokenize each file path on '/' and '_' (also '-', '.'); compute BM25 over
the resulting segment-bag-of-words against issue query tokens. Returns a
per-file 0-1 score suitable as a multiplicative boost on top of v7.4's
content-BM25 file ranking.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from groundtruth.pretask.v2_types import QueryObject

_PATH_SPLIT_RE = re.compile(r"[/\\_\-\.]+")
_SNAKE_JOIN_RE = re.compile(r"[/\\\-\.]+")
_CODE_EXT_RE = re.compile(
    r"\.(py|pyi|js|jsx|ts|tsx|go|rs|java|kt|rb|c|h|hpp|cpp|cc|cs|swift|m|mm|php|scala|sh|bash|zsh|sql|yaml|yml|json|toml|md)$",
    re.IGNORECASE,
)
_BM25_K1 = 1.5
_BM25_B = 0.75


def _strip_extension(file_path: str) -> str:
    return _CODE_EXT_RE.sub("", file_path)


def _tokenize_path(file_path: str) -> list[str]:
    """Split path into lowercased segment tokens. Drop empty + extension.

    Produces atomic split tokens AND the joined snake_case form so qualifiers
    like ``linear_model`` match against ``sklearn/linear_model/ridge.py``.
    """
    stripped = _strip_extension(file_path).lower()
    atomic: list[str] = [t for t in _PATH_SPLIT_RE.split(stripped) if t]
    snake_segments: list[str] = [s for s in _SNAKE_JOIN_RE.split(stripped) if s and "_" in s]
    seen: set[str] = set()
    out: list[str] = []
    for tok in atomic + snake_segments:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def score_path_segment_match(
    candidate_files: list[str],
    query_tokens: list[tuple[str, float]],
) -> dict[str, float]:
    """Return {file_path: bm25_score in [0, 1]} where 1.0 is best in this set.

    Returns 0.0 for files with no token matches. Returns empty dict if no
    query tokens. Normalizes by max so scores are comparable across calls.
    """
    if not candidate_files:
        return {}
    if not query_tokens:
        return {f: 0.0 for f in candidate_files}

    doc_tokens: dict[str, list[str]] = {f: _tokenize_path(f) for f in candidate_files}
    doc_counters: dict[str, Counter[str]] = {f: Counter(toks) for f, toks in doc_tokens.items()}
    doc_lens: dict[str, int] = {f: len(toks) for f, toks in doc_tokens.items()}
    n_docs = len(candidate_files)
    avgdl = (sum(doc_lens.values()) / n_docs) if n_docs else 0.0

    seen_q: dict[str, float] = {}
    for tok, weight in query_tokens:
        key = tok.strip().lower()
        if not key:
            continue
        if key not in seen_q or weight > seen_q[key]:
            seen_q[key] = weight

    df: dict[str, int] = {}
    for key in seen_q:
        df[key] = sum(1 for f in candidate_files if doc_counters[f].get(key, 0) > 0)

    raw_scores: dict[str, float] = {}
    for f in candidate_files:
        dl = doc_lens[f]
        score = 0.0
        for key, weight in seen_q.items():
            n = df.get(key, 0)
            if n == 0:
                continue
            tf = doc_counters[f].get(key, 0)
            if tf == 0:
                continue
            idf = math.log(1.0 + (n_docs - n + 0.5) / (n + 0.5))
            denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (dl / avgdl if avgdl > 0 else 0.0))
            tf_component = (tf * (_BM25_K1 + 1.0)) / denom if denom > 0 else 0.0
            score += weight * idf * tf_component
        raw_scores[f] = score

    max_score = max(raw_scores.values()) if raw_scores else 0.0
    if max_score <= 0.0:
        return {f: 0.0 for f in candidate_files}
    return {f: (s / max_score) for f, s in raw_scores.items()}


def score_from_query(
    candidate_files: list[str],
    query: QueryObject,
) -> dict[str, float]:
    """Convenience wrapper. Builds query_tokens from query.high_signal_tokens
    + query.file_hints (each at weight 4.0) + query.function_hints (weight 2.0)
    + query.class_hints (weight 2.0). Then calls score_path_segment_match.
    """
    query_tokens: list[tuple[str, float]] = []
    for hst in query.high_signal_tokens:
        query_tokens.append((hst.token, hst.weight))
    for fh in query.file_hints:
        query_tokens.append((fh, 4.0))
        stem = _strip_extension(fh)
        for piece in _PATH_SPLIT_RE.split(stem):
            if piece:
                query_tokens.append((piece, 4.0))
    for fn in query.function_hints:
        query_tokens.append((fn, 2.0))
    for cls in query.class_hints:
        query_tokens.append((cls, 2.0))
    return score_path_segment_match(candidate_files, query_tokens)
