"""Stage A anchor selection for v7.4 brief.

Selects trusted anchor files from which graph expansion starts:
  1. Semantic top-K: files whose first-500-token summary has the highest
     cosine similarity to the issue text embedding.
  2. Symbol-anchor rule: files containing a symbol whose normalized form
     matches any normalized token from the issue text.

Anchors marked as trusted (semantic_score >= TAU_ANCHOR or symbol match) seed
the BFS in graph_reach.py. Untrusted anchors stay in the candidate set but do
not seed graph expansion.
"""
from __future__ import annotations

import hashlib
import pickle
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Minimum identifier length to consider as a potential symbol match.
_MIN_TOKEN_LEN = 3


@dataclass
class AnchorRecord:
    path: str
    semantic_score: float
    reason: str  # "semantic_top_k" | "symbol_match" | "both"
    trusted_for_expansion: bool


def _normalize_identifier(name: str) -> list[str]:
    """Split any identifier into lowercase word parts.

    Handles snake_case, camelCase, PascalCase, kebab-case.
    """
    # Split on underscores and hyphens
    parts = re.split(r"[_\-]", name)
    result: list[str] = []
    for part in parts:
        # Split camelCase / PascalCase on case boundaries
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]|\d+", part)
        if words:
            result.extend(w.lower() for w in words if len(w) >= _MIN_TOKEN_LEN)
        else:
            low = part.lower()
            if len(low) >= _MIN_TOKEN_LEN:
                result.append(low)
    return result


def _extract_issue_tokens(issue_text: str) -> set[str]:
    """Extract potential identifier tokens from issue text."""
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", issue_text)
    tokens = {t for t in raw if len(t) >= _MIN_TOKEN_LEN}
    return tokens


def _issue_word_parts(issue_text: str) -> set[str]:
    """Normalized word parts from all identifiers in the issue text."""
    tokens = _extract_issue_tokens(issue_text)
    parts: set[str] = set()
    for tok in tokens:
        parts.update(_normalize_identifier(tok))
    return parts


def _symbol_anchors(
    issue_text: str,
    graph_db: str,
    k_anchor: int,
) -> dict[str, str]:
    """Return {file_path: reason} for symbol-matched anchors.

    Containment match: symbol's normalized parts ⊆ issue's normalized parts.
    """
    issue_parts = _issue_word_parts(issue_text)
    if not issue_parts:
        return {}

    conn = sqlite3.connect(graph_db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT DISTINCT name, file_path FROM nodes WHERE is_test = 0")
    rows = c.fetchall()
    conn.close()

    matched: dict[str, list[str]] = {}  # file_path -> list[matched_symbol_names]
    for row in rows:
        sym_name: str = row["name"] or ""
        file_path: str = row["file_path"] or ""
        if not sym_name or not file_path:
            continue
        sym_parts = set(_normalize_identifier(sym_name))
        if not sym_parts:
            continue
        if sym_parts <= issue_parts:
            matched.setdefault(file_path, []).append(sym_name)

    # Sort by number of matched symbols (more matches = stronger anchor)
    ranked = sorted(matched.items(), key=lambda kv: len(kv[1]), reverse=True)
    return {fp: "symbol_match" for fp, _ in ranked[:k_anchor]}


_EMBED_CACHE: dict[str, tuple[list[str], np.ndarray]] = {}


def _file_summary(file_path: str, repo_root: str, max_chars: int = 600) -> str:
    """Return first max_chars of a file (~150 tokens — within model's 256-token window)."""
    full = Path(repo_root) / file_path
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""
    return text[:max_chars]


def _embed(texts: list[str], model: object) -> np.ndarray:
    """Encode texts using a sentence-transformers model."""
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False,
                        batch_size=128)  # type: ignore[union-attr]


def _cache_key(graph_db: str) -> str:
    db_path = Path(graph_db)
    stat = db_path.stat() if db_path.exists() else None
    sig = f"{graph_db}:{stat.st_mtime if stat else 0}:{stat.st_size if stat else 0}"
    return hashlib.md5(sig.encode()).hexdigest()


def _get_file_embeddings(
    graph_db: str,
    repo_root: str,
    model: object,
) -> tuple[list[str], np.ndarray]:
    """Return (file_paths, embeddings) for all non-test files. Cached in memory."""
    key = _cache_key(graph_db)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]

    # Try disk cache
    cache_dir = Path(graph_db).parent / ".embed_cache"
    cache_file = cache_dir / f"{key}.pkl"
    if cache_file.exists():
        with open(cache_file, "rb") as f:
            result = pickle.load(f)
            _EMBED_CACHE[key] = result
            return result

    conn = sqlite3.connect(graph_db)
    c = conn.cursor()
    c.execute("SELECT DISTINCT file_path FROM nodes WHERE is_test = 0")
    file_paths = [row[0] for row in c.fetchall() if row[0]]
    conn.close()

    summaries = [_file_summary(fp, repo_root) for fp in file_paths]
    nonempty_idx = [i for i, s in enumerate(summaries) if s.strip()]

    if not nonempty_idx:
        result = (file_paths, np.zeros((len(file_paths), 384), dtype=np.float32))
        _EMBED_CACHE[key] = result
        return result

    sums_nonempty = [summaries[i] for i in nonempty_idx]
    embs = _embed(sums_nonempty, model)

    # Build full embedding matrix (zero for empty files)
    full_embs = np.zeros((len(file_paths), embs.shape[1]), dtype=np.float32)
    for i, orig_i in enumerate(nonempty_idx):
        full_embs[orig_i] = embs[i]

    result = (file_paths, full_embs)
    _EMBED_CACHE[key] = result

    # Save disk cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "wb") as f:
        pickle.dump(result, f)

    return result


def semantic_top_k(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    model: object,
    k_sem_top: int = 20,
) -> dict[str, float]:
    """Return {file_path: cosine_score} for the top-K semantically similar files."""
    file_paths, file_embs = _get_file_embeddings(graph_db, repo_root, model)
    if not file_paths:
        return {}

    issue_emb = _embed([issue_text], model)[0]
    scores = file_embs @ issue_emb  # cosine (normalized embeddings)

    ranked = sorted(zip(file_paths, scores.tolist()), key=lambda x: x[1], reverse=True)
    return {fp: float(score) for fp, score in ranked[:k_sem_top]}


def select_anchors(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    model: object,
    *,
    k_anchor: int = 5,
    k_sem_top: int = 20,
    tau_anchor: float = 0.30,
) -> tuple[list[AnchorRecord], dict[str, float]]:
    """Run Stage A anchor selection.

    Returns:
        (anchors, semantic_top_k_scores)
        anchors: all anchor records (semantic + symbol), sorted by semantic score
        semantic_top_k_scores: full {file: score} map for Stage B semantic term
    """
    sem_scores = semantic_top_k(issue_text, repo_root, graph_db, model, k_sem_top=k_sem_top)
    sym_files = _symbol_anchors(issue_text, graph_db, k_anchor=k_anchor)

    # Merge: files in both get reason="both"
    anchor_map: dict[str, dict] = {}

    for fp, score in sem_scores.items():
        anchor_map[fp] = {
            "path": fp,
            "semantic_score": score,
            "reason": "semantic_top_k",
            "trusted_for_expansion": score >= tau_anchor,
        }

    for fp in sym_files:
        if fp in anchor_map:
            anchor_map[fp]["reason"] = "both"
            anchor_map[fp]["trusted_for_expansion"] = True  # symbol match always trusted
        else:
            sem_score = sem_scores.get(fp, 0.0)
            anchor_map[fp] = {
                "path": fp,
                "semantic_score": sem_score,
                "reason": "symbol_match",
                "trusted_for_expansion": True,
            }

    anchors = [AnchorRecord(**v) for v in anchor_map.values()]
    anchors.sort(key=lambda a: a.semantic_score, reverse=True)
    return anchors, sem_scores
