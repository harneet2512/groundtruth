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
from groundtruth.memory.enrich.embed import (
    PASSAGE_CACHE_VERSION,
    _PASSAGE_VEC_CACHE,
    aggregate_symbol_cosines,
    model_identity,
    passage_hash,
    read_agg_params,
    symbol_passage,
)

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


# Per-graph in-memory cache: (file_paths, {file_path -> symbol-vector matrix}).
# Each value is an (n_symbols, dim) float32 array of the file's per-symbol vectors;
# semantic_top_k aggregates them by MaxSim against the issue vector.
_EMBED_CACHE: dict[str, tuple[list[str], dict[str, np.ndarray]]] = {}

# Content-addressed per-symbol vector cache (survives across graphs/runs): keyed by
# sha256(version:model:dim:passage). One ONNX encode per UNIQUE passage, ever.
# SHARED STORE (encode-blowup fix 2026-06-09): this is the SAME bounded-LRU object
# as embed._PASSAGE_VEC_CACHE, so the other semantic half
# (graph_localizer._semantic_score_by_file) hits vectors this half already paid
# for within the task — and vice versa. The local name is kept for back-compat
# (tests clear it via anchor_select._SYMVEC_CACHE).
_SYMVEC_CACHE = _PASSAGE_VEC_CACHE


def _model_identity(model: object) -> tuple[str, int]:
    """Best-effort (model_name, dim) for the passage cache key. Delegates to the
    single shared implementation (embed.model_identity) so both semantic halves
    key the content-addressed vector cache IDENTICALLY — a fork here would split
    the cache and silently double the encode work."""
    return model_identity(model)


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


# Bump when the file-summary CONTENT changes so stale embeddings (keyed by graph
# mtime) are invalidated.
#   sym1     = per-FILE symbol-bag summary (one vector/file; was raw text[:600]).
#   sym2-fn  = per-SYMBOL passages aggregated by MaxSim (CHANGE 1) — one vector per
#              indexed symbol, file score = MaxSim over its symbols. Bumping past
#              sym1 abandons every stale file-bag .pkl so the two shapes never mix.
# Single-sourced from embed.PASSAGE_CACHE_VERSION (2026-06-09) so the shared
# passage-vector cache key can never drift between the two semantic halves.
_SUMMARY_VERSION = PASSAGE_CACHE_VERSION


def _cache_key(graph_db: str, model_name: str = "", dim: int = 0) -> str:
    """Cache key for the per-graph file-embedding matrices.

    MODEL-KEYED (bug fix 2026-06-09): the key folds in the embedder IDENTITY
    (model name + dim) alongside the graph signature. Before this, the key was
    md5(db:mtime:size:version) ONLY — a gte<->e5 model switch on the same graph
    short-circuited into the OTHER model's matrices (memory dict + .embed_cache
    pkl), which either dim-crashes the matmul (384 vs 768) or, worse, silently
    scores with stale foreign-model vectors. The identity is computed BEFORE any
    cache lookup (see _get_file_embeddings)."""
    db_path = Path(graph_db)
    stat = db_path.stat() if db_path.exists() else None
    sig = (
        f"{graph_db}:{stat.st_mtime if stat else 0}:{stat.st_size if stat else 0}:"
        f"{_SUMMARY_VERSION}:{model_name}:{dim}"
    )
    return hashlib.md5(sig.encode()).hexdigest()


def _matrices_match_dim(file_matrix: dict, dim: int) -> bool:
    """True iff EVERY cached per-file matrix is a 2-D array whose vector width
    equals the CURRENT model's dim. A pkl written under a different-dim model (or
    a corrupted one) is treated as a cache MISS and recomputed — never consumed
    (correct-or-quiet: stale vectors must not silently rank files)."""
    try:
        for m in file_matrix.values():
            arr = np.asarray(m)
            if arr.ndim != 2 or int(arr.shape[1]) != int(dim):
                return False
        return True
    except Exception:
        return False


def _get_file_embeddings(
    graph_db: str,
    repo_root: str,
    model: object,
) -> tuple[list[str], dict[str, np.ndarray]]:
    """Return (file_paths, {file_path -> (n_symbols, dim) symbol-vector matrix}).

    CHANGE 1 — symbol-level granularity. Instead of ONE vector per file from a
    concatenated symbol-bag (which averages the issue function into its siblings
    and clusters sibling files at cosine 0.80-0.84), embed each non-test SYMBOL as
    its own short ``"{name} {signature}\\n{behavioral props}"`` passage and keep the
    per-symbol vectors. ``semantic_top_k`` then scores a file by the MAX cosine over
    its symbols (ColBERT MaxSim, Khattab & Zaharia SIGIR 2020 + MaxP, Dai & Callan
    SIGIR 2019) so the file holding the gold function is no longer diluted.

    A file with ZERO indexed symbols falls back to its ``_file_summary`` text as ONE
    passage (a strict superset of today's behaviour). Empty/blank passages are never
    embedded (correct-or-quiet). Cached in memory AND, per UNIQUE passage, in
    ``_SYMVEC_CACHE`` so only cache-misses are encoded (one batched ONNX pass)."""
    # Model identity FIRST — before any cache lookup — so the cache key is
    # model-keyed and a gte<->e5 switch can never reuse the other model's
    # matrices (bug fix 2026-06-09: the key previously had NO model identity and
    # both the memory dict and the .embed_cache pkl short-circuited before
    # _model_identity ran).
    model_name, dim = _model_identity(model)
    key = _cache_key(graph_db, model_name, dim)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]

    # Try disk cache
    cache_dir = Path(graph_db).parent / ".embed_cache"
    cache_file = cache_dir / f"{key}.pkl"
    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                result = pickle.load(f)
            # Validate the on-disk shape matches the sym2-fn contract (a dict of
            # per-file matrices); a stale sym1 .pkl (np.ndarray value) is ignored.
            # ALSO validate every matrix's vector WIDTH against the CURRENT model
            # dim — a stale different-dim pkl is a MISS, never silently consumed.
            if (
                isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[1], dict)
                and _matrices_match_dim(result[1], dim)
            ):
                _EMBED_CACHE[key] = result
                return result
        except Exception:
            pass  # corrupt/legacy cache -> recompute below (correct-or-quiet)

    conn = sqlite3.connect(graph_db)
    c = conn.cursor()
    c.execute("SELECT DISTINCT file_path FROM nodes WHERE is_test = 0")
    # Ingress point (semantic): canonicalize before these become the keys of the
    # cosine map (semantic_top_k -> sem_scores) so they match the symbol + lexical
    # pipes (#18). dict.fromkeys preserves order and de-dups paths that differ only
    # by separator/prefix.
    file_paths = list(dict.fromkeys(_norm_path(row[0]) for row in c.fetchall() if row[0]))

    # Collect this node's behavioral properties (docstring / call_order / guards /
    # conditional_return) per node_id so each symbol's passage carries its own body
    # snippet — NOT the whole file's. Source = the SAME properties the file-bag used,
    # regrouped per-symbol. (No file reads: stays inside the demand-scope cost bound.)
    node_body: dict[int, list[str]] = {}
    try:
        for _nid, _val in c.execute(
            "SELECT p.node_id, p.value FROM properties p JOIN nodes n ON n.id=p.node_id "
            "WHERE n.is_test=0 AND p.kind IN ('docstring','call_order','guard_clause','conditional_return')"
        ):
            if _nid is None:
                continue
            lst = node_body.setdefault(int(_nid), [])
            if len(lst) < 4:  # a few snippets keep the passage inside the token cap
                lst.append(str(_val))
    except sqlite3.Error:
        pass  # properties table absent on older graphs -> name+signature passage only

    # Per-file ordered list of symbol passages (carry the existing 60/symbol cap).
    file_passages: dict[str, list[str]] = {fp: [] for fp in file_paths}
    for _id, _fp, _nm, _sig in c.execute(
        "SELECT id, file_path, name, COALESCE(signature,'') FROM nodes WHERE is_test = 0"
    ):
        _k = _norm_path(_fp)
        if not _k or _k not in file_passages or len(file_passages[_k]) >= 60:
            continue
        body = " ".join(node_body.get(int(_id), [])) if _id is not None else ""
        passage = symbol_passage(_nm or "", _sig or "", body)
        if passage:  # correct-or-quiet: never embed a blank symbol
            file_passages[_k].append(passage)
    conn.close()

    # Files with NO indexed symbols fall back to the file_summary text as ONE passage
    # (superset of current behaviour). Empty stays empty (zero-vector file).
    for fp in file_paths:
        if not file_passages[fp]:
            fb = symbol_passage(_file_summary(fp, repo_root), "")
            if fb:
                file_passages[fp] = [fb]

    # Gather the UNIQUE passages that miss the content-addressed vector cache, embed
    # them ONCE in a single batched ONNX pass, store by passage-hash. `vec_by_hash`
    # pins THIS call's vectors locally (hits AND fresh encodes) so the shared
    # bounded-LRU cache's eviction can never drop a vector between lookup and
    # matrix assembly below.
    vec_by_hash: dict[str, np.ndarray] = {}
    miss_hashes: list[str] = []
    miss_passages: list[str] = []
    seen_miss: set[str] = set()
    for fp in file_paths:
        for passage in file_passages[fp]:
            h = passage_hash(passage, model_name, dim, _SUMMARY_VERSION)
            if h in vec_by_hash or h in seen_miss:
                continue
            cached = _SYMVEC_CACHE.get(h)
            if cached is not None:
                vec_by_hash[h] = np.asarray(cached, dtype=np.float32)
            else:
                seen_miss.add(h)
                miss_hashes.append(h)
                miss_passages.append(passage)
    if miss_passages:
        new_embs = _embed(miss_passages, model)  # PASSAGE prefix (is_query=False)
        for h, vec in zip(miss_hashes, new_embs):
            v = np.asarray(vec, dtype=np.float32)
            vec_by_hash[h] = v
            _SYMVEC_CACHE[h] = v

    # Assemble the per-file symbol-vector matrices from this call's pinned vectors.
    file_matrix: dict[str, np.ndarray] = {}
    for fp in file_paths:
        vecs = [
            vec_by_hash[passage_hash(p, model_name, dim, _SUMMARY_VERSION)]
            for p in file_passages[fp]
        ]
        if vecs:
            file_matrix[fp] = np.vstack(vecs).astype(np.float32)
        else:
            file_matrix[fp] = np.zeros((0, dim), dtype=np.float32)

    result = (file_paths, file_matrix)
    _EMBED_CACHE[key] = result

    # Save disk cache (best-effort; a read-only dir must not break the brief).
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "wb") as f:
            pickle.dump(result, f)
    except Exception:
        pass

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
    widening the candidate set.

    CHANGE 1: each file now scores by ColBERT MaxSim over its per-symbol vectors —
    ``alpha*max_i(cos_i) + (1-alpha)*mean(top_k cos_i)`` (``aggregate_symbol_cosines``)
    — so the gold function is not averaged into 60 siblings. The return CONTRACT is
    byte-identical: ``dict[file_path -> float]`` in [0, 1]."""
    file_paths, file_matrix = _get_file_embeddings(graph_db, repo_root, model)
    if not file_paths:
        return {}

    issue_emb = _embed([issue_text], model, is_query=True)[0]  # e5: the issue is the QUERY
    issue_emb = np.asarray(issue_emb, dtype=np.float32)
    alpha, top_k = read_agg_params()

    file_scores: list[tuple[str, float]] = []
    for fp in file_paths:
        mat = file_matrix.get(fp)
        if mat is None or mat.shape[0] == 0:
            file_scores.append((fp, 0.0))
            continue
        # Per-symbol cosines (vectors are unit-normalized: dot == cosine), then MaxSim.
        cosines = (mat @ issue_emb).tolist()
        cosines = [c for c in cosines if math.isfinite(c)]
        score = aggregate_symbol_cosines(cosines, alpha=alpha, top_k=top_k)
        file_scores.append((fp, float(score)))

    ranked = sorted(file_scores, key=lambda x: x[1], reverse=True)
    if score_all:
        # Full component-score map: keep only finite, strictly-positive scores
        # (correct-or-quiet — never surface 0/NaN as a semantic signal).
        return {
            fp: float(score)
            for fp, score in ranked
            if math.isfinite(score) and score > 0.0
        }
    # SEED map: same strictly-positive discipline as the component map (fix
    # 2026-06-09). A zero/negative-cosine file carries NO semantic evidence —
    # admitting it as a "semantic_top_k" SEED (anchor + candidate membership)
    # injected up to k_sem_top no-signal files whenever the embedder was dead or
    # the corpus mismatched (a zero embedder now yields an EMPTY seed map, not
    # 20 fake semantic anchors). Correct-or-quiet at the filter level.
    return {
        fp: float(score)
        for fp, score in ranked[:k_sem_top]
        if math.isfinite(score) and score > 0.0
    }


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
