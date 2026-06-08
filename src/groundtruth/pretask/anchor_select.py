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
import math
import pickle
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.hybrid import lexical_file_search

# Minimum identifier length to consider as a potential symbol match.
_MIN_TOKEN_LEN = 3


def _norm_path(path: str) -> str:
    """Canonicalize a file path to the project-wide form before it is used as a
    dict key.

    Identical to the normalizer every other pretask stage uses
    (``v7_4_brief.py:548``, ``graph_localizer.py:590``, ``v1r_brief.py``):
    backslashes → forward slashes, then strip any leading ``./`` / ``/``. This is
    the ONE place that previously keyed the multi-signal anchor merge off RAW
    ``nodes.file_path`` while the lexical pipe was already forward-slashed in
    ``hybrid.py`` — so on a Windows-indexed graph the same physical file split
    into two ``AnchorRecord``s and the trust-upgrade merge silently never fired.
    Normalizing all three ingress points (semantic / symbol / lexical) to this
    canonical form keeps the merge keys on-contract."""
    return path.replace("\\", "/").lstrip("./").lstrip("/")


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
        # Ingress point (symbol): canonicalize before keying the merge dict so the
        # symbol pipe's keys match the semantic + lexical pipes (#18).
        file_path: str = _norm_path(row["file_path"] or "")
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


def _embed(texts: list[str], model: object, *, is_query: bool = False) -> np.ndarray:
    """Encode texts with whatever embedder API is present.

    Supports BOTH sentence-transformers (`.encode`) AND the container ONNX `EmbeddingModel`
    (`.embed_batch` / `.embed`). run_v74's anchor selection MUST use the SAME ONNX surface as
    localize (BRIEFING invariant 2: semantic ON in BOTH halves — a half-on pipeline gives
    worthless numbers). ROOT BUG (run13 ap=0): `.encode()` raised on the ONNX model, so semantic
    anchor selection silently failed and issue-named golds (arviz plot_hdi) were never anchored.
    e5 is query/passage-asymmetric, so the issue is embedded as a QUERY, files as PASSAGES."""
    if hasattr(model, "encode"):
        return np.asarray(model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False, batch_size=128
        ))  # type: ignore[union-attr]
    if hasattr(model, "embed_batch"):
        return np.asarray(model.embed_batch(list(texts), is_query=is_query), dtype=np.float32)  # type: ignore[union-attr]
    if hasattr(model, "embed"):
        return np.asarray([model.embed(t, is_query=is_query) for t in texts], dtype=np.float32)  # type: ignore[union-attr]
    raise AttributeError(f"embedder {type(model).__name__} exposes no encode/embed_batch/embed")


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
    # Ingress point (semantic): canonicalize before these become the keys of the
    # cosine map (semantic_top_k -> sem_scores) so they match the symbol + lexical
    # pipes (#18). dict.fromkeys preserves order and de-dups paths that differ only
    # by separator/prefix.
    file_paths = list(dict.fromkeys(_norm_path(row[0]) for row in c.fetchall() if row[0]))
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
    *,
    score_all: bool = False,
) -> dict[str, float]:
    """Return {file_path: cosine_score} for semantically similar files.

    The cosine of EVERY indexed file is computed (one matmul: ``file_embs @
    issue_emb``); ``k_sem_top`` only controls how many of those scores are
    RETURNED. Two distinct uses are deliberately decoupled by the caller:

      * ``score_all=False`` (default): return the top-``k_sem_top`` slice — the
        bounded SEED set that becomes candidate-set membership (no flooding).
      * ``score_all=True``: return the FULL score map (every file with a finite,
        strictly-positive cosine). This is the COMPONENT-score source: it lets a
        candidate already in the set (via graph / BM25 / path) carry its REAL
        cosine in ``components['sem']`` instead of a spurious 0. Without it the
        ``sem`` component is structurally zero on every candidate outside the
        top-``k_sem_top``, which makes a present-but-unconsumed embedder
        indistinguishable from a genuinely-zero one. Correct-or-quiet: a file
        whose cosine is <= 0 or non-finite is omitted (never injected as fact),
        so a truly-zero embedder yields an empty map exactly as before.

    The full map is keyed identically to the bounded slice, so a candidate's
    component lookup (`sem_all.get(fp, 0.0)`) returns its real cosine whether or
    not it made the seed cut. The DESIGN INTENT (this function's original
    docstring: "full {file: score} map for Stage B") is restored without
    widening the candidate set."""
    file_paths, file_embs = _get_file_embeddings(graph_db, repo_root, model)
    if not file_paths:
        return {}

    issue_emb = _embed([issue_text], model, is_query=True)[0]  # e5: the issue is the QUERY
    scores = file_embs @ issue_emb  # cosine (normalized embeddings)

    ranked = sorted(zip(file_paths, scores.tolist()), key=lambda x: x[1], reverse=True)
    if score_all:
        # Full component-score map: keep only finite, strictly-positive cosines
        # (correct-or-quiet — never surface 0/NaN as a semantic signal).
        return {
            fp: float(score)
            for fp, score in ranked
            if math.isfinite(score) and score > 0.0
        }
    return {fp: float(score) for fp, score in ranked[:k_sem_top]}


def select_anchors(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    model: object,
    *,
    k_anchor: int = 5,
    k_sem_top: int = 20,
    k_lex_top: int = 10,
    tau_anchor: float = 0.30,
) -> tuple[list[AnchorRecord], dict[str, float], dict[str, float]]:
    """Run Stage A anchor selection.

    Three signals merged:
      1. Semantic top-K: cosine similarity between issue embedding and file summaries.
      2. Symbol anchors: files containing symbols whose normalized form matches issue tokens.
      3. Lexical top-K: BM25-style term overlap between issue text and file content.

    Returns:
        (anchors, sem_seed_scores, sem_all_scores)
        anchors: all anchor records sorted by semantic score.
        sem_seed_scores: the bounded top-``k_sem_top`` map — drives candidate-set
          SEED membership (kept small so semantics cannot flood the candidate set).
        sem_all_scores: the FULL {file: cosine} map (every file with a finite,
          strictly-positive cosine) — the COMPONENT-score source so a candidate
          already in the set carries its REAL ``components['sem']`` instead of a
          spurious 0. The two are decoupled on purpose: widening the component
          coverage must NOT widen what the agent sees. Both come from one cached
          embedding matmul (``_get_file_embeddings`` memoises the encode).
    """
    sem_seed_scores = semantic_top_k(
        issue_text, repo_root, graph_db, model, k_sem_top=k_sem_top
    )
    # Full cosine map for the component term (no seed effect). Cheap: reuses the
    # cached file embeddings, only re-runs the matmul + sort.
    sem_all_scores = semantic_top_k(
        issue_text, repo_root, graph_db, model, score_all=True
    )
    # Anchor/seed logic operates on the bounded seed map (unchanged behaviour).
    sem_scores = sem_seed_scores
    sym_files = _symbol_anchors(issue_text, graph_db, k_anchor=k_anchor)

    # Lexical top-K via BM25 (reuses validated v7.3 signal)
    lex_hits = lexical_file_search(
        issue_text, repo_root, graph_db, IssueAnchors(), max_files=k_lex_top
    )
    # Ingress point (lexical): hybrid.py forward-slashes h.file but does NOT strip
    # the leading ./ or / — run it through the same canonical normalizer so the
    # lexical keys match the semantic + symbol pipes (#18).
    lex_files = {_norm_path(h.file) for h in lex_hits}

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
            anchor_map[fp]["trusted_for_expansion"] = True
        else:
            anchor_map[fp] = {
                "path": fp,
                "semantic_score": sem_scores.get(fp, 0.0),
                "reason": "symbol_match",
                "trusted_for_expansion": True,
            }

    for fp in lex_files:
        if fp in anchor_map:
            # Upgrade trust for files already found by another signal
            anchor_map[fp]["trusted_for_expansion"] = True
            if "lexical" not in anchor_map[fp]["reason"]:
                anchor_map[fp]["reason"] += "+lexical"
        else:
            anchor_map[fp] = {
                "path": fp,
                "semantic_score": sem_scores.get(fp, 0.0),
                "reason": "lexical",
                "trusted_for_expansion": True,
            }

    anchors = [AnchorRecord(**v) for v in anchor_map.values()]
    anchors.sort(key=lambda a: a.semantic_score, reverse=True)
    return anchors, sem_seed_scores, sem_all_scores
