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
import os
import re
import sqlite3
import sys
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

# Canonical quantization for the semantic top-k MEMBERSHIP cut (FIX 2, defense-in-depth).
# The seed set is `ranked[:k_sem_top]`, ordered by raw cosine. Two files whose cosines differ
# only by float-reduction noise (~1e-7 across thread-pool orders) can straddle the k-th slot,
# so which file lands IN the seed set ??? a candidate-set-level flip ??? depends on reduction order.
# That breaks brief determinism (the k_sem_top INTEGER flip). Quantizing the ORDERING score to
# a fixed grid collapses sub-epsilon differences to an exact tie, which the deterministic
# file-path tiebreak then resolves identically across runs. 4dp matches the precedent already
# used for the localizer sort key (graph_localizer.py `_sem_led_key` rounds to 4). It changes
# NO ranking semantics beyond sub-epsilon ties: cosines that genuinely differ at 1e-4 or coarser
# keep their order; only noise-level ties (<5e-5) are merged, and the RETURNED score is the raw
# cosine ??? the quantization touches the membership boundary only, never the stored value.
_SEM_MEMBERSHIP_QUANT_DP = 4


def _norm_path(path: str) -> str:
    """Canonicalize a file path to the project-wide form before it is used as a
    dict key.

    Identical to the normalizer every other pretask stage uses
    (``v7_4_brief.py:548``, ``graph_localizer.py:590``, ``v1r_brief.py``):
    backslashes ??? forward slashes, then strip any leading ``./`` / ``/``. This is
    the ONE place that previously keyed the multi-signal anchor merge off RAW
    ``nodes.file_path`` while the lexical pipe was already forward-slashed in
    ``hybrid.py`` ??? so on a Windows-indexed graph the same physical file split
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

    Containment match: symbol's normalized parts ??? issue's normalized parts.
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
    ranked = sorted(matched.items(), key=lambda item: (-len(item[1]), item[0]))
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
# for within the task ??? and vice versa. The local name is kept for back-compat
# (tests clear it via anchor_select._SYMVEC_CACHE).
_SYMVEC_CACHE = _PASSAGE_VEC_CACHE


def _shared_passage_cache():
    """Return the current shared cache, repairing aliases after module reloads.

    Some long-lived/test processes reload ``memory.enrich.embed`` to re-read
    configuration flags. Reloading reconstructs its bounded LRU; a static alias
    here would then write to an orphaned cache while the localizer reads the new
    one, doubling every encode. Production normally imports once, so this is a
    no-op there.
    """
    from groundtruth.memory.enrich import embed as _embed_module

    global _SYMVEC_CACHE
    current = _embed_module._PASSAGE_VEC_CACHE
    if _SYMVEC_CACHE is not current:
        _SYMVEC_CACHE = current
    return current


def _model_identity(model: object) -> tuple[str, int]:
    """Best-effort (model_name, dim) for the passage cache key. Delegates to the
    single shared implementation (embed.model_identity) so both semantic halves
    key the content-addressed vector cache IDENTICALLY ??? a fork here would split
    the cache and silently double the encode work."""
    return model_identity(model)


def _file_summary(file_path: str, repo_root: str, max_chars: int = 600) -> str:
    """Return first max_chars of a file (~150 tokens ??? within model's 256-token window)."""
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
    localize (BRIEFING invariant 2: semantic ON in BOTH halves ??? a half-on pipeline gives
    worthless numbers). ROOT BUG (run13 ap=0): `.encode()` raised on the ONNX model, so semantic
    anchor selection silently failed and issue-named golds (arviz plot_hdi) were never anchored.
    e5 is query/passage-asymmetric, so the issue is embedded as a QUERY, files as PASSAGES."""
    if hasattr(model, "encode"):
        # BUG-8 (2026-06-15): pass the role EXPLICITLY so the ONNX adapter never has to
        # infer query-vs-passage from len(texts). A sentence-transformers .encode is
        # symmetric (ignores the kwarg); the _OnnxEmbedderAdapter consumes it. Wrapped
        # so a SentenceTransformer that rejects the unknown kwarg still works.
        # _OnnxEmbedderAdapter.encode accepts is_query; sentence-transformers'
        # .encode rejects unknown kwargs with TypeError (signature) OR ValueError
        # (its own kwarg validation) ??? fall back to the symmetric call for ST, which
        # has no query/passage asymmetry to thread anyway.
        try:
            return np.asarray(
                model.encode(
                    texts,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                    batch_size=128,
                    is_query=is_query,
                )
            )  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return np.asarray(
                model.encode(
                    texts, normalize_embeddings=True, show_progress_bar=False, batch_size=128
                )
            )  # type: ignore[union-attr]
    if hasattr(model, "embed_batch"):
        return np.asarray(model.embed_batch(list(texts), is_query=is_query), dtype=np.float32)  # type: ignore[union-attr]
    if hasattr(model, "embed"):
        return np.asarray([model.embed(t, is_query=is_query) for t in texts], dtype=np.float32)  # type: ignore[union-attr]
    raise AttributeError(f"embedder {type(model).__name__} exposes no encode/embed_batch/embed")


# Bump when the file-summary CONTENT changes so stale embeddings (keyed by graph
# mtime) are invalidated.
#   sym1     = per-FILE symbol-bag summary (one vector/file; was raw text[:600]).
#   sym2-fn  = per-SYMBOL passages aggregated by MaxSim (CHANGE 1) ??? one vector per
#              indexed symbol, file score = MaxSim over its symbols. Bumping past
#              sym1 separates the process-local matrix cache from the old file-bag shape.
# Single-sourced from embed.PASSAGE_CACHE_VERSION (2026-06-09) so the shared
# passage-vector cache key can never drift between the two semantic halves.
_SUMMARY_VERSION = PASSAGE_CACHE_VERSION


def _cache_key(
    graph_db: str,
    model_name: str = "",
    dim: int = 0,
    *,
    body_on: bool | None = None,
) -> str:
    """Cache key for the process-local per-graph file-embedding matrices.

    MODEL-KEYED (bug fix 2026-06-09): the key folds in the embedder IDENTITY
    (model name + dim) alongside the graph signature. Before this, the key was
    md5(db:mtime:size:version) ONLY ??? a gte<->e5 model switch on the same graph
    short-circuited into the OTHER model's in-memory matrices, which either
    dim-crashes the matmul (384 vs 768) or, worse, silently
    scores with stale foreign-model vectors. The identity is computed BEFORE any
    cache lookup (see _get_file_embeddings)."""
    db_path = Path(graph_db)
    stat = db_path.stat() if db_path.exists() else None
    if body_on is None:
        body_on = os.environ.get("GT_SEM_BODY", "") not in ("", "0", "false", "no")
    sig = (
        f"{graph_db}:{stat.st_mtime if stat else 0}:{stat.st_size if stat else 0}:"
        f"{_SUMMARY_VERSION}:{model_name}:{dim}:sem_body={int(body_on)}"
    )
    return hashlib.md5(sig.encode()).hexdigest()


def _anchor_passage_budget() -> int:
    """``GT_SEM_PASSAGE_BUDGET``: hard per-call ENCODE budget for the anchor-half
    whole-repo symbol embed (encode-blowup fix 2026-06-16 ??? the SAME 29/113-SIGKILL
    failure mode the localizer half closed 2026-06-09, but on this UNCAPPED half:
    ``_get_file_embeddings`` embeds EVERY non-test file's symbols, so a big repo
    (thousands of files x <=60 passages) sent millions of passages to the embedder in
    one call and ballooned the HOST RSS ~15GB -> OOM on a 16GB runner). Counts fresh
    passages SENT to the embedder this call ??? cache hits are free. Reads the SAME env
    var + default (4096) as graph_localizer._sem_passage_budget so ONE knob bounds both
    semantic halves. Clamped to >=1 so there is never silent infinite work; a malformed
    value falls back to the default (correct-or-quiet)."""
    default = 4096
    raw = os.environ.get("GT_SEM_PASSAGE_BUDGET")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return default


def _budget_priority_order(
    file_paths: list[str],
    file_passages: dict[str, list[str]],
    issue_text: str,
) -> list[str]:
    """Order ``file_paths`` so the most issue-relevant files are visited FIRST by the
    encode budget ??? the structural fix for the budget skipping by raw DB-row order.

    Relevance = the count of issue normalized word-parts that appear in the file's
    symbol passages OR its path's normalized parts (the SAME lexical signal
    ``_symbol_anchors`` uses). This is a CHEAP pure-Python pass over passages already
    built in memory ??? no DB, no embed, inside the demand-scope cost bound.

    Stable: a higher-overlap file sorts ahead of a lower-overlap one; non-empty
    overlap ties use canonical path. When ``issue_text`` is
    blank (no issue tokens) EVERY file scores 0 overlap, so the order is byte-
    identical to the input ??? issue-less / warm-cache callers are unchanged.
    Generalized: pure lexical token overlap, no gold labels, no benchmark logic."""
    issue_parts = _issue_word_parts(issue_text)
    if not issue_parts:
        return list(file_paths)

    def _overlap(fp: str) -> int:
        parts: set[str] = set()
        # Path segments contribute their normalized parts (a file named for the
        # subject ??? e.g. importer.py for an "importer" issue ??? ranks up even with
        # no symbol-name hit).
        for seg in fp.replace("\\", "/").split("/"):
            seg = seg.rsplit(".", 1)[0]  # drop extension
            parts.update(_normalize_identifier(seg))
        # Symbol-name parts: the passage head is "{name} {signature}"; take the
        # leading identifier-ish tokens of each passage so we don't tokenize whole
        # bodies (bounded work).
        for passage in file_passages.get(fp, ()):
            head = passage.split("\n", 1)[0]
            for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", head):
                parts.update(_normalize_identifier(tok))
        return len(parts & issue_parts)

    # Higher overlap first; canonical path is the final tie-break. Raw DB order
    # must not decide which equal-overlap file spends a bounded encode slot.
    return sorted(file_paths, key=lambda fp: (-_overlap(fp), _norm_path(fp)))


def _get_file_embeddings(
    graph_db: str,
    repo_root: str,
    model: object,
    issue_text: str = "",
) -> tuple[list[str], dict[str, np.ndarray]]:
    """Return (file_paths, {file_path -> (n_symbols, dim) symbol-vector matrix}).

    CHANGE 1 ??? symbol-level granularity. Instead of ONE vector per file from a
    concatenated symbol-bag (which averages the issue function into its siblings
    and clusters sibling files at cosine 0.80-0.84), embed each non-test SYMBOL as
    its own short ``"{name} {signature}\\n{behavioral props}"`` passage and keep the
    per-symbol vectors. ``semantic_top_k`` then scores a file by the MAX cosine over
    its symbols (ColBERT MaxSim, Khattab & Zaharia SIGIR 2020 + MaxP, Dai & Callan
    SIGIR 2019) so the file holding the gold function is no longer diluted.

    A file with ZERO indexed symbols falls back to its ``_file_summary`` text as ONE
    passage (a strict superset of today's behaviour). Empty/blank passages are never
    embedded (correct-or-quiet). Cached in process memory and, per UNIQUE passage,
    in ``_SYMVEC_CACHE`` so only cache-misses are encoded (one batched ONNX pass).

    The assembled matrix is intentionally not persisted beside ``graph.db``. A
    matrix-only cache cannot restore the shared per-passage cache used by the
    downstream bounded graph-localizer pass, so its presence changed acquisition
    results across otherwise identical processes.
    """
    # Model identity FIRST ??? before any cache lookup ??? so the cache key is
    # model-keyed and a gte<->e5 switch can never reuse the other model's
    # matrices (bug fix 2026-06-09: the key previously had NO model identity and
    # the memory dict short-circuited before _model_identity ran).
    model_name, dim = _model_identity(model)
    _body_on = os.environ.get("GT_SEM_BODY", "") not in ("", "0", "false", "no")
    key = _cache_key(graph_db, model_name, dim, body_on=_body_on)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]

    conn = sqlite3.connect(graph_db)
    c = conn.cursor()
    c.execute("SELECT DISTINCT file_path FROM nodes WHERE is_test = 0")
    # Ingress point (semantic): canonicalize before these become the keys of the
    # cosine map (semantic_top_k -> sem_scores) so they match the symbol + lexical
    # pipes (#18). De-duplicate separator/prefix variants, then sort so DB
    # insertion order cannot reach a bounded semantic cut.
    file_paths = sorted(dict.fromkeys(_norm_path(row[0]) for row in c.fetchall() if row[0]))

    # Per-symbol passage BODY comes from the SHARED assembler (the SAME source the
    # localizer half ??? graph_localizer._assemble_symbol_passages ??? reads), so a given
    # symbol produces IDENTICAL passage text and passage_hash in BOTH halves under
    # GT_SEM_BODY (one shared-cache encode, never two). OFF (default) it is the
    # docstring/call_order/guard/conditional_return join ??? byte-identical to before.
    # Imported at call time: no module-level import edge to graph_localizer, no cycle.
    from groundtruth.pretask.graph_localizer import _symbol_body_map

    body_map = _symbol_body_map(conn, _body_on)

    # Per-file source-identity-ordered symbol passages (carry the existing 60 cap).
    file_passages: dict[str, list[str]] = {fp: [] for fp in file_paths}
    _symbol_rows = c.execute(
        # The cap keeps whichever rows arrive first. Stable source identity below,
        # rather than node insertion id, decides which rows survive across rebuilds.
        "SELECT id, file_path, name, COALESCE(signature,''), start_line, end_line "
        "FROM nodes WHERE is_test = 0"
    ).fetchall()
    _symbol_rows.sort(
        key=lambda row: (
            _norm_path(str(row[1] or "")),
            row[4] is None,
            int(row[4] or 0),
            row[5] is None,
            int(row[5] or 0),
            str(row[2] or ""),
            str(row[3] or ""),
            int(row[0] or 0),
        )
    )
    for _id, _fp, _nm, _sig, _sl, _el in _symbol_rows:
        _k = _norm_path(_fp)
        if not _k or _k not in file_passages or len(file_passages[_k]) >= 60:
            continue
        body = body_map.get(int(_id), "") if _id is not None else ""
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
    # HARD ENCODE BUDGET (encode-blowup fix 2026-06-16): without this the anchor half
    # embedded EVERY non-test file's symbols in ONE batch ??? a large repo sent millions
    # of passages and ballooned the HOST process ~15GB -> OOM on a 16GB runner. Cap the
    # number of FRESH (cache-missing) passages SENT to the embedder, exactly as
    # graph_localizer._semantic_score_by_file does. The budget visit is ordered by
    # issue relevance (see _budget_priority_order below), so over-budget passages are
    # the LEAST issue-relevant files; they simply stay unscored (their file gets its
    # symbols' available vectors, or the zero-vector fallback below) ??? bounded beats
    # killed, correct-or-quiet. Cache hits are FREE (already paid), so warm caches
    # still score every file.
    _budget = _anchor_passage_budget()
    _n_skipped = 0
    # ENCODE-BUDGET PRIORITY (bug fix 2026-06-17): the budget truncates from the
    # BACK of the iteration order, so whichever files are visited LAST stay unscored
    # on a >budget repo. `file_paths` is `SELECT DISTINCT file_path` ??? raw DB-ROW
    # order, NOT relevance ??? so on a big repo the issue's own candidate/anchor files
    # can land in the skipped tail and score 0.0 (the gold file unranked, defeating
    # the MaxSim granularity fix on exactly the repos where it matters). Unlike the
    # localizer half (graph_localizer._semantic_score_by_file), which receives `files`
    # ALREADY in caller-priority order, this half builds file_paths itself with no
    # priority signal. Order the budget visit by issue-token overlap (the SAME
    # lexical signal _symbol_anchors uses: a file's symbols + path normalized parts
    # intersected with the issue's normalized parts), highest overlap first; ties use
    # canonical path so equivalent graph insertions spend the same bounded slots.
    # issue_text="" preserves the supplied order; the current DB ingress is already
    # canonicalized, so warm-cache and issue-less callers remain deterministic.
    # This only reorders WHICH
    # files spend the cold-path encode budget ??? the returned file_paths list and the
    # path-keyed score map are unchanged (generalized, no gold labels, no benchmark
    # logic).
    _budget_order = _budget_priority_order(file_paths, file_passages, issue_text)
    shared_cache = _shared_passage_cache()
    for fp in _budget_order:
        for passage in file_passages[fp]:
            h = passage_hash(passage, model_name, dim, _SUMMARY_VERSION)
            if h in vec_by_hash or h in seen_miss:
                continue
            cached = shared_cache.get(h)
            if cached is not None:
                vec_by_hash[h] = np.asarray(cached, dtype=np.float32)
            elif len(miss_passages) < _budget:
                seen_miss.add(h)
                miss_hashes.append(h)
                miss_passages.append(passage)
            else:
                # Over budget: this passage stays unscored (never re-counted).
                _n_skipped += 1
    if miss_passages:
        new_embs = _embed(miss_passages, model)  # PASSAGE prefix (is_query=False)
        for h, vec in zip(miss_hashes, new_embs):
            v = np.asarray(vec, dtype=np.float32)
            vec_by_hash[h] = v
            shared_cache[h] = v
    if _n_skipped > 0:
        # Correct-or-quiet: ONE line, stderr only (never agent-visible stdout).
        print(
            f"[GT_SEM] anchor passage budget hit ({len(miss_passages)}/"
            f"{len(miss_passages) + _n_skipped} fresh passages encoded; "
            f"{_n_skipped} over-budget passages skipped)",
            file=sys.stderr,
        )

    # Assemble the per-file symbol-vector matrices from this call's pinned vectors.
    file_matrix: dict[str, np.ndarray] = {}
    for fp in file_paths:
        # A passage skipped by the encode budget has no vector ??? skip it (None) so the
        # file scores on its remaining symbols rather than KeyError-ing the brief.
        vecs = [
            v
            for v in (
                vec_by_hash.get(passage_hash(p, model_name, dim, _SUMMARY_VERSION))
                for p in file_passages[fp]
            )
            if v is not None
        ]
        if vecs:
            file_matrix[fp] = np.vstack(vecs).astype(np.float32)
        else:
            file_matrix[fp] = np.zeros((0, dim), dtype=np.float32)

    result = (file_paths, file_matrix)
    _EMBED_CACHE[key] = result

    return result


def _semantic_body_files(graph_db: str) -> set[str]:
    """Files whose capped anchor passages differ under ``GT_SEM_BODY``.

    This recomputes only passage provenance, never vectors.  It therefore works
    for both fresh encodes and memory/disk cache hits while preserving the public
    embedding return contract.
    """
    if os.environ.get("GT_SEM_BODY", "") in ("", "0", "false", "no"):
        return set()
    from groundtruth.pretask.graph_localizer import _symbol_body_map

    conn = sqlite3.connect(graph_db)
    try:
        enriched: set[int] = set()
        _symbol_body_map(conn, True, enriched)
        counts: dict[str, int] = {}
        files: set[str] = set()
        for nid, file_path in conn.execute(
            "SELECT id, file_path FROM nodes WHERE is_test=0 ORDER BY id"
        ):
            fp = _norm_path(file_path)
            if not fp or counts.get(fp, 0) >= 60:
                continue
            counts[fp] = counts.get(fp, 0) + 1
            if int(nid) in enriched:
                files.add(fp)
        return files
    finally:
        conn.close()


def semantic_top_k(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    model: object,
    k_sem_top: int = 20,
    *,
    score_all: bool = False,
    body_enriched_files_out: set[str] | None = None,
) -> dict[str, float]:
    """Return {file_path: cosine_score} for semantically similar files.

    The cosine of EVERY indexed file is computed (one matmul: ``file_embs @
    issue_emb``); ``k_sem_top`` only controls how many of those scores are
    RETURNED. Two distinct uses are deliberately decoupled by the caller:

      * ``score_all=False`` (default): return the top-``k_sem_top`` slice ??? the
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

    CHANGE 1: each file now scores by ColBERT MaxSim over its per-symbol vectors ???
    ``alpha*max_i(cos_i) + (1-alpha)*mean(top_k cos_i)`` (``aggregate_symbol_cosines``)
    ??? so the gold function is not averaged into 60 siblings. The return CONTRACT is
    byte-identical: ``dict[file_path -> float]`` in [0, 1]."""
    file_paths, file_matrix = _get_file_embeddings(graph_db, repo_root, model, issue_text)
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
        # Correct-or-quiet: a cached matrix whose embedding dim != the query dim (e.g. an
        # e5/384 memory-store cache meeting a gte/768 brief query) cannot be compared --
        # ABSTAIN (score 0.0, fall to lexical) instead of crashing the WHOLE localization
        # on a matmul shape error. The dim mismatch is a config issue; the code degrades.
        if getattr(mat, "ndim", 0) != 2 or mat.shape[1] != issue_emb.shape[0]:
            file_scores.append((fp, 0.0))
            continue
        # Per-symbol cosines (vectors are unit-normalized: dot == cosine), then MaxSim.
        cosines = (mat @ issue_emb).tolist()
        cosines = [c for c in cosines if math.isfinite(c)]
        score = aggregate_symbol_cosines(cosines, alpha=alpha, top_k=top_k)
        file_scores.append((fp, float(score)))

    # FIX 2: order the membership cut by the QUANTIZED score (raw value preserved in the tuple
    # and in the returned map). Quantizing the sort key collapses float-reduction-noise ties so
    # `ranked[:k_sem_top]` selects the same files regardless of the reduction order; the exact
    # file-path secondary key breaks the resulting ties deterministically.
    ranked = sorted(
        file_scores,
        key=lambda item: (-round(item[1], _SEM_MEMBERSHIP_QUANT_DP), item[0]),
    )
    if score_all:
        # Full component-score map: keep only finite, strictly-positive scores
        # (correct-or-quiet ??? never surface 0/NaN as a semantic signal).
        result = {fp: float(score) for fp, score in ranked if math.isfinite(score) and score > 0.0}
        if body_enriched_files_out is not None:
            body_enriched_files_out.update(set(result) & _semantic_body_files(graph_db))
        return result
    # SEED map: same strictly-positive discipline as the component map (fix
    # 2026-06-09). A zero/negative-cosine file carries NO semantic evidence ???
    # admitting it as a "semantic_top_k" SEED (anchor + candidate membership)
    # injected up to k_sem_top no-signal files whenever the embedder was dead or
    # the corpus mismatched (a zero embedder now yields an EMPTY seed map, not
    # 20 fake semantic anchors). Correct-or-quiet at the filter level.
    result = {
        fp: float(score) for fp, score in ranked[:k_sem_top] if math.isfinite(score) and score > 0.0
    }
    if body_enriched_files_out is not None:
        body_enriched_files_out.update(set(result) & _semantic_body_files(graph_db))
    return result


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
    body_enriched_files_out: set[str] | None = None,
) -> tuple[list[AnchorRecord], dict[str, float], dict[str, float]]:
    """Run Stage A anchor selection.

    Three signals merged:
      1. Semantic top-K: cosine similarity between issue embedding and file summaries.
      2. Symbol anchors: files containing symbols whose normalized form matches issue tokens.
      3. Lexical top-K: BM25-style term overlap between issue text and file content.

    Returns:
        (anchors, sem_seed_scores, sem_all_scores)
        anchors: all anchor records sorted by semantic score.
        sem_seed_scores: the bounded top-``k_sem_top`` map ??? drives candidate-set
          SEED membership (kept small so semantics cannot flood the candidate set).
        sem_all_scores: the FULL {file: cosine} map (every file with a finite,
          strictly-positive cosine) ??? the COMPONENT-score source so a candidate
          already in the set carries its REAL ``components['sem']`` instead of a
          spurious 0. The two are decoupled on purpose: widening the component
          coverage must NOT widen what the agent sees. Both come from one cached
          embedding matmul (``_get_file_embeddings`` memoises the encode).
    """
    sem_seed_scores = semantic_top_k(
        issue_text,
        repo_root,
        graph_db,
        model,
        k_sem_top=k_sem_top,
    )
    # Full cosine map for the component term (no seed effect). Cheap: reuses the
    # cached file embeddings, only re-runs the matmul + sort.
    sem_all_scores = semantic_top_k(
        issue_text,
        repo_root,
        graph_db,
        model,
        score_all=True,
        body_enriched_files_out=body_enriched_files_out,
    )
    # Anchor/seed logic operates on the bounded seed map (unchanged behaviour).
    sem_scores = sem_seed_scores
    sym_files = _symbol_anchors(issue_text, graph_db, k_anchor=k_anchor)

    # Lexical top-K via BM25 (reuses validated v7.3 signal)
    lex_hits = lexical_file_search(
        issue_text, repo_root, graph_db, IssueAnchors(), max_files=k_lex_top
    )
    # Ingress point (lexical): hybrid.py forward-slashes h.file but does NOT strip
    # the leading ./ or / ??? run it through the same canonical normalizer so the
    # lexical keys match the semantic + symbol pipes (#18).
    # lexical_file_search already returns relevance rank. De-duplicate in that
    # first-seen order; a set made equal-semantic-score anchors depend on each
    # process's randomized hash seed, breaking independent acquisition identity.
    lex_files = list(dict.fromkeys(_norm_path(h.file) for h in lex_hits))

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
    # Stable sorting retains each producer's order inside equal-score groups;
    # lexical ties therefore keep the deterministic relevance order above.
    anchors.sort(key=lambda a: a.semantic_score, reverse=True)
    return anchors, sem_seed_scores, sem_all_scores
