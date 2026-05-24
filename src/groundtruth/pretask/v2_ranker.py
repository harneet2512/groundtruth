"""Track B v2 ranker: file ranking (B1) + function ranking (B2).

B1 is a thin adapter over v7.4 (ablation C). B2 scores every Function/Method
node in the top-50 files using six signals fused via Reciprocal Rank Fusion
(Cormack et al. 2009). Direct-mention is weighted 2x; all other signals
weight 1. Signals: direct, file, bm25, sem, caller_prox, callee_prop.

Callee_prop fires on functions reachable from the top-K initial-RRF functions
via outgoing CALLS edges (confidence ≥ 0.5). This complements the existing
caller_prox signal — together they form a bidirectional reach prior.
"""
from __future__ import annotations

import hashlib
import math
import os
import pickle
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from groundtruth.pretask.v2_types import (
    QueryObject,
    RankedFile,
    RankedFunction,
    RankedResults,
)

_BM25_K1 = 1.5
_BM25_B = 0.75

_MIN_CONFIDENCE = 0.5
_MAX_FILES = 50

_DEMOTE_DIR_PREFIXES = (
    "examples/", "example/", "demo/", "demos/", "docs/", "doc/",
    "samples/", "sample/", "tutorial/", "tutorials/",
    "benchmarks/", "benchmark/", "fixtures/",
)


def _is_demoted_path(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("./").lstrip("/").lower()
    return any(p.startswith(d) for d in _DEMOTE_DIR_PREFIXES)
_TOP_FUNCTIONS = 100
_BODY_PEEK_LINES = 10
_BODY_PEEK_CHARS = 200
_TOK_RE = re.compile(r"[a-zA-Z0-9_]+")

_RRF_K = 60
_RRF_WEIGHTS: dict[str, float] = {
    "direct": 2.0,
    "file": 1.0,
    "bm25": 1.0,
    "sem": 1.0,
    "caller_prox": 1.0,
    "callee_prop": 1.0,
}
_CALLEE_PROP_TOP_K = 10

# v2.2-6: multi-hop callee BFS depth (default 1 = current behavior, no regression).
# Setting GT_V22_MULTIHOP=2 or 3 enables multi-hop reach. Confidence-gated at every hop.
def _multihop_depth() -> int:
    try:
        return max(1, int(os.environ.get("GT_V22_MULTIHOP", "1")))
    except (TypeError, ValueError):
        return 1


# v2.2-7: global function-level BM25 (default off = per-file, no regression).
def _global_bm25_enabled() -> bool:
    return os.environ.get("GT_V22_GLOBAL_BM25") == "1"


_CACHE_DIR = Path(".embed_cache") / "funcv2"


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOK_RE.findall(text) if t]


_V22_TIER1_ENABLED = os.environ.get("GT_V22_TIER1") == "1"

_V22_FILE_RRF_WEIGHTS: dict[str, float] = {
    "v74": float(os.environ.get("GT_V22_W_V74", "2.0")),
    "path_seg": float(os.environ.get("GT_V22_W_PATHSEG", "1.0")),
    "test_link": float(os.environ.get("GT_V22_W_TESTLINK", "1.0")),
    "augment": float(os.environ.get("GT_V22_W_AUGMENT", "1.0")),
}


def _all_indexed_files(graph_db_path: str) -> list[str]:
    """Return all distinct file_paths in the graph (the full candidate pool)."""
    if not os.path.exists(graph_db_path):
        return []
    try:
        conn = sqlite3.connect(graph_db_path)
        try:
            rows = conn.execute("SELECT DISTINCT file_path FROM nodes").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return sorted({row[0] for row in rows if row[0]})


def _augment_stem_match_scores(
    candidate_files: list[str],
    augmented_tokens: list[tuple[str, float]],
) -> dict[str, float]:
    """Score each file by max IDF-weight of any augmented token matching its stem.

    augmented_tokens carries the IDF weight set by query_augment (high-rare → high
    weight). A file gets the max weight of any token that exact-matches its stem.
    """
    if not augmented_tokens:
        return {}
    tok_w = {tok.lower(): w for tok, w in augmented_tokens if tok}
    if not tok_w:
        return {}
    raw: dict[str, float] = {}
    for f in candidate_files:
        stem = Path(f).stem.lower()
        if not stem:
            continue
        w = tok_w.get(stem, 0.0)
        if w > 0.0:
            raw[f] = w
    if not raw:
        return {}
    peak = max(raw.values())
    if peak <= 0.0:
        return {}
    return {f: v / peak for f, v in raw.items()}


def rank_files(
    query: QueryObject,
    repo_path: str,
    graph_db_path: str,
) -> list[RankedFile]:
    augmented_tokens: list[tuple[str, float]] = []
    if _V22_TIER1_ENABLED:
        from groundtruth.pretask.query_augment import augment_query_with_graph

        original_tokens = {(t.token, t.source) for t in query.high_signal_tokens}
        query = augment_query_with_graph(query, query.raw_text or "", graph_db_path)
        for t in query.high_signal_tokens:
            if (t.token, t.source) not in original_tokens:
                augmented_tokens.append((t.token, float(t.weight)))

    hint_blob = ""
    hints = list(query.file_hints) + list(query.function_hints) + list(query.class_hints)
    if hints:
        hint_blob = "Hints: " + ", ".join(hints) + "\n\n"
    issue_text = hint_blob + (query.raw_text or "")
    if not issue_text.strip():
        return []
    from groundtruth.pretask.v7_4_brief import run_v74

    result = run_v74(
        issue_text=issue_text,
        repo_root=repo_path,
        graph_db=graph_db_path,
        ablation="C",
    )
    v74_scores: dict[str, float] = {
        entry["path"]: float(entry["score"]) for entry in result.ranked_full
    }

    if not _V22_TIER1_ENABLED:
        out: list[RankedFile] = []
        for entry in result.ranked_full[:_MAX_FILES]:
            out.append(RankedFile(file=entry["path"], score=float(entry["score"])))
        return out

    candidate_files = _all_indexed_files(graph_db_path)
    if not candidate_files:
        return [
            RankedFile(file=entry["path"], score=float(entry["score"]))
            for entry in result.ranked_full[:_MAX_FILES]
        ]

    from groundtruth.pretask.path_segment import score_from_query as _path_seg_score
    from groundtruth.pretask.test_link import score_test_to_source

    ps = _path_seg_score(candidate_files, query)
    ts = score_test_to_source(candidate_files, query)
    aug = _augment_stem_match_scores(candidate_files, augmented_tokens)

    v74_signal: list[float] = []
    ps_signal: list[float] = []
    ts_signal: list[float] = []
    aug_signal: list[float] = []
    for f in candidate_files:
        v74_signal.append(max(0.0, v74_scores.get(f, 0.0)))
        ps_signal.append(ps.get(f, 0.0))
        ts_signal.append(ts.get(f, 0.0))
        aug_signal.append(aug.get(f, 0.0))

    signals = {
        "v74": v74_signal,
        "path_seg": ps_signal,
        "test_link": ts_signal,
        "augment": aug_signal,
    }
    rrf = _rrf_score(signals, k=_RRF_K, weights=_V22_FILE_RRF_WEIGHTS)
    for i, f in enumerate(candidate_files):
        if _is_demoted_path(f):
            rrf[i] *= 0.1
    order = sorted(range(len(candidate_files)), key=lambda i: -rrf[i])
    out_v22: list[RankedFile] = []
    for idx in order[:_MAX_FILES]:
        if rrf[idx] <= 0.0:
            break
        out_v22.append(RankedFile(file=candidate_files[idx], score=rrf[idx]))
    return out_v22


def _read_body_snippet(repo_path: str, file_path: str, start_line: int, end_line: int) -> str:
    if start_line is None or start_line <= 0:
        return ""
    end = end_line if end_line and end_line > start_line else start_line + _BODY_PEEK_LINES
    end = min(end, start_line + _BODY_PEEK_LINES)
    abs_path = os.path.join(repo_path, file_path)
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as fh:
            lines: list[str] = []
            for i, line in enumerate(fh, start=1):
                if i < start_line:
                    continue
                if i > end:
                    break
                lines.append(line)
        body = "".join(lines)
        return body[:_BODY_PEEK_CHARS]
    except OSError:
        return ""


def _doc_text(name: str, signature: str | None, body: str) -> str:
    parts = [name, signature or "", body]
    return " ".join(p for p in parts if p)


def _bm25_score_per_file(
    docs: list[list[str]],
    query_terms: list[tuple[str, float]],
) -> list[float]:
    n_docs = len(docs)
    if n_docs == 0:
        return []
    doc_lens = [len(d) for d in docs]
    avgdl = sum(doc_lens) / n_docs if n_docs else 0.0
    df: Counter[str] = Counter()
    for d in docs:
        for term in set(d):
            df[term] += 1
    raw_scores: list[float] = []
    for d, dl in zip(docs, doc_lens):
        tf: Counter[str] = Counter(d)
        score = 0.0
        for term, weight in query_terms:
            term_l = term.lower()
            if term_l not in tf:
                continue
            f = tf[term_l]
            n = df.get(term_l, 0)
            idf = math.log(1 + (n_docs - n + 0.5) / (n + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 1.0))
            num = f * (_BM25_K1 + 1)
            score += weight * idf * (num / denom if denom else 0.0)
        raw_scores.append(max(0.0, score))
    max_score = max(raw_scores) if raw_scores else 0.0
    if max_score <= 0.0:
        return [0.0] * n_docs
    return [s / max_score for s in raw_scores]


def _bm25_score_global(
    docs: list[list[str]],
    query_terms: list[tuple[str, float]],
) -> list[float]:
    """Global function-level BM25: single pass over the FULL candidate set.

    Differs from _bm25_score_per_file in that df, avgdl, and max-normalization
    are computed over all docs (function-level), not per-file. This makes
    cross-file scores directly comparable — see docs/v22_research_review.md
    defect §4.

    Inputs/outputs match _bm25_score_per_file: tokenized docs aligned with
    query_terms (term, weight). Returns max-normalized scores in [0, 1].
    """
    n_docs = len(docs)
    if n_docs == 0:
        return []
    doc_lens = [len(d) for d in docs]
    avgdl = sum(doc_lens) / n_docs if n_docs else 0.0
    df: Counter[str] = Counter()
    for d in docs:
        for term in set(d):
            df[term] += 1
    raw_scores: list[float] = []
    for d, dl in zip(docs, doc_lens):
        tf: Counter[str] = Counter(d)
        score = 0.0
        for term, weight in query_terms:
            term_l = term.lower()
            if term_l not in tf:
                continue
            f = tf[term_l]
            n = df.get(term_l, 0)
            idf = math.log(1 + (n_docs - n + 0.5) / (n + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 1.0))
            num = f * (_BM25_K1 + 1)
            score += weight * idf * (num / denom if denom else 0.0)
        raw_scores.append(max(0.0, score))
    max_score = max(raw_scores) if raw_scores else 0.0
    if max_score <= 0.0:
        return [0.0] * n_docs
    return [s / max_score for s in raw_scores]


def _cache_key(graph_db_path: str, file_path: str) -> Path:
    abs_db = os.path.abspath(graph_db_path)
    h = hashlib.sha1((abs_db + "::" + file_path).encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{h}.pkl"


def _load_emb_cache(path: Path) -> dict[str, list[float]] | None:
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, pickle.UnpicklingError, EOFError):
        return None
    return None


def _save_emb_cache(path: Path, data: dict[str, list[float]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(data, fh)
    except OSError:
        pass


def _cosine(a: Any, b: Any) -> float:
    import numpy as np

    arr_a = np.asarray(a, dtype="float32")
    arr_b = np.asarray(b, dtype="float32")
    na = float(np.linalg.norm(arr_a))
    nb = float(np.linalg.norm(arr_b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(arr_a, arr_b) / (na * nb))


def _score_caller_prox(
    function_id: int,
    db_path: str,
    file_to_rank: dict[str, int],
) -> float:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_file FROM edges "
            "WHERE target_id = ? AND confidence >= ?",
            (function_id, _MIN_CONFIDENCE),
        ).fetchall()
    finally:
        conn.close()
    best = 0.0
    for (src,) in rows:
        if src is None:
            continue
        rk = file_to_rank.get(src)
        if rk is None:
            continue
        if rk <= 10:
            best = max(best, 0.3)
        elif rk <= 50:
            best = max(best, 0.1)
    return best


def _find_callees_set(
    source_func_ids: list[int],
    db_path: str,
    depth: int = 1,
) -> set[int]:
    """Return target_ids reachable via outgoing CALLS edges from any source function.

    BFS to ``depth`` hops, confidence-gated at every hop (>= _MIN_CONFIDENCE).
    Edges below the floor are not traversed, so a low-confidence hop blocks all
    further descendants on that path.

    Cycle-safe: a node already discovered (or already visited as a source) is
    not re-traversed, so cycles like A->B->A terminate at the configured depth.

    depth=1 reproduces the original single-hop behavior — same SQL shape and
    same result set — so existing callers see no regression.
    """
    if not source_func_ids or depth < 1:
        return set()
    sources = {sid for sid in source_func_ids if sid is not None}
    if not sources:
        return set()
    discovered: set[int] = set()
    visited: set[int] = set(sources)  # don't re-traverse out of already-seen nodes
    frontier: set[int] = sources
    conn = sqlite3.connect(db_path)
    try:
        for _hop in range(depth):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"SELECT DISTINCT target_id FROM edges "
                f"WHERE source_id IN ({placeholders}) AND confidence >= ?",
                (*frontier, _MIN_CONFIDENCE),
            ).fetchall()
            next_frontier: set[int] = set()
            for (tid,) in rows:
                if tid is None:
                    continue
                discovered.add(tid)
                if tid not in visited:
                    visited.add(tid)
                    next_frontier.add(tid)
            frontier = next_frontier
    finally:
        conn.close()
    return discovered


def _rrf_score(
    signal_scores: dict[str, list[float]],
    k: int = _RRF_K,
    weights: dict[str, float] | None = None,
) -> list[float]:
    """Weighted reciprocal rank fusion across multiple signal score arrays.

    Items with score <= 0 in a signal contribute nothing from that signal
    (treated as unranked). This avoids spurious bonuses from score=0 ties
    in sparse signals like direct-mention and caller_prox.
    """
    if not signal_scores:
        return []
    first = next(iter(signal_scores.values()))
    n = len(first)
    if n == 0:
        return []
    out = [0.0] * n
    w_map = weights or {}
    for sig, scores in signal_scores.items():
        if len(scores) != n:
            continue
        w = w_map.get(sig, 1.0)
        order = sorted(range(n), key=lambda i: (-scores[i], i))
        for rank, idx in enumerate(order, start=1):
            if scores[idx] <= 0.0:
                continue
            out[idx] += w * (1.0 / (k + rank))
    return out


def rank_functions(
    query: QueryObject,
    ranked_files: list[RankedFile],
    repo_path: str,
    graph_db_path: str,
) -> list[RankedFunction]:
    if not ranked_files:
        return []
    top_files = ranked_files[:_MAX_FILES]
    file_to_score: dict[str, float] = {f.file: f.score for f in top_files}
    file_to_rank: dict[str, int] = {f.file: i + 1 for i, f in enumerate(top_files)}
    paths = list(file_to_score.keys())
    if not paths:
        return []
    placeholders = ",".join("?" for _ in paths)

    conn = sqlite3.connect(graph_db_path)
    try:
        rows = conn.execute(
            f"SELECT id, name, file_path, signature, start_line, end_line "
            f"FROM nodes WHERE label IN ('Function','Method') "
            f"AND file_path IN ({placeholders})",
            paths,
        ).fetchall()
    finally:
        conn.close()

    by_file: dict[str, list[tuple[int, str, str | None, int, int]]] = {}
    for fid, name, fpath, sig, sline, eline in rows:
        by_file.setdefault(fpath, []).append((fid, name, sig, sline or 0, eline or 0))

    direct_set: set[str] = set(query.function_hints) | set(query.class_hints)
    direct_set_lower: set[str] = {s.lower() for s in direct_set}

    query_terms = [(t.token, float(t.weight)) for t in query.high_signal_tokens]

    model = None
    raw_text_emb = None
    code_block_embs: list[Any] = []
    sem_available = bool(query.raw_text or query.code_blocks)
    if sem_available:
        try:
            from groundtruth.pretask.v7_4_brief import _get_model

            model = _get_model()
            if query.raw_text:
                raw_text_emb = model.encode([query.raw_text], normalize_embeddings=False)[0]
            for cb in query.code_blocks:
                code_block_embs.append(model.encode([cb], normalize_embeddings=False)[0])
        except Exception:
            model = None
            raw_text_emb = None
            code_block_embs = []
            sem_available = False

    func_ids: list[int] = []
    func_names: list[str] = []
    func_files: list[str] = []
    score_direct: list[float] = []
    score_file: list[float] = []
    score_bm25: list[float] = []
    score_sem: list[float] = []
    score_prox: list[float] = []

    # v2.2-7: when global BM25 is enabled, accumulate all docs across files
    # and compute one BM25 pass at the end. When disabled, keep per-file BM25
    # exactly as before (no regression).
    use_global_bm25 = _global_bm25_enabled()
    all_docs_global: list[list[str]] = []  # only populated when use_global_bm25 is True

    for fpath, funcs in by_file.items():
        file_score = file_to_score[fpath]

        docs: list[list[str]] = []
        doc_strs: list[str] = []
        for fid, name, sig, sline, eline in funcs:
            body = _read_body_snippet(repo_path, fpath, sline, eline)
            doc_str = _doc_text(name, sig, body)
            doc_strs.append(doc_str)
            docs.append(_tokenize(doc_str))

        if use_global_bm25:
            # Defer BM25 until all files are seen — fill placeholder zeros now.
            bm25_scores: list[float] = [0.0] * len(funcs)
            all_docs_global.extend(docs)
        else:
            bm25_scores = _bm25_score_per_file(docs, query_terms) if query_terms else [0.0] * len(funcs)

        sem_scores: list[float] = [0.0] * len(funcs)
        if sem_available and model is not None:
            cache_path = _cache_key(graph_db_path, fpath)
            cache = _load_emb_cache(cache_path) or {}
            cache_dirty = False
            func_embs: list[Any] = []
            for (fid, name, sig, sline, eline), doc_str in zip(funcs, doc_strs):
                first_line = doc_str.split("\n", 1)[0][:200] if doc_str else name
                emb_key = f"{fid}::{first_line}"
                if emb_key in cache:
                    func_embs.append(cache[emb_key])
                else:
                    emb = model.encode([first_line or name], normalize_embeddings=False)[0]
                    cache[emb_key] = list(map(float, emb.tolist())) if hasattr(emb, "tolist") else list(emb)
                    func_embs.append(emb)
                    cache_dirty = True
            if cache_dirty:
                _save_emb_cache(cache_path, cache)

            for i, fe in enumerate(func_embs):
                best = 0.0
                if raw_text_emb is not None:
                    best = max(best, _cosine(raw_text_emb, fe))
                for cb_emb in code_block_embs:
                    best = max(best, _cosine(cb_emb, fe))
                sem_scores[i] = best

        for i, (fid, name, sig, sline, eline) in enumerate(funcs):
            direct_hit = (name in direct_set) or (name.lower() in direct_set_lower)
            sd = (5.0 * file_score) if direct_hit else 0.0
            sb = bm25_scores[i] if i < len(bm25_scores) else 0.0
            ss = sem_scores[i] if i < len(sem_scores) else 0.0
            sp = _score_caller_prox(fid, graph_db_path, file_to_rank)

            func_ids.append(fid)
            func_names.append(name)
            func_files.append(fpath)
            score_direct.append(sd)
            score_file.append(file_score)
            score_bm25.append(sb)
            score_sem.append(ss)
            score_prox.append(sp)

    # v2.2-7: global BM25 pass over the full candidate function set.
    # The append order above matches the order docs were extended into
    # all_docs_global, so indexes line up 1:1 with score_bm25.
    if use_global_bm25 and query_terms and all_docs_global:
        global_scores = _bm25_score_global(all_docs_global, query_terms)
        for i, gs in enumerate(global_scores):
            if i < len(score_bm25):
                score_bm25[i] = gs

    n = len(func_ids)
    if n == 0:
        return []

    signals_initial = {
        "direct": score_direct,
        "file": score_file,
        "bm25": score_bm25,
        "sem": score_sem,
        "caller_prox": score_prox,
    }
    rrf_initial = _rrf_score(signals_initial, k=_RRF_K, weights=_RRF_WEIGHTS)

    top_indices = sorted(range(n), key=lambda i: -rrf_initial[i])[:_CALLEE_PROP_TOP_K]
    top_func_ids = [func_ids[i] for i in top_indices]
    callees = _find_callees_set(top_func_ids, graph_db_path, depth=_multihop_depth())
    func_id_to_idx = {fid: i for i, fid in enumerate(func_ids)}
    score_callee_prop: list[float] = [0.0] * n
    for cid in callees:
        idx = func_id_to_idx.get(cid)
        if idx is not None:
            score_callee_prop[idx] = 1.0

    signals_final = dict(signals_initial)
    signals_final["callee_prop"] = score_callee_prop
    rrf_final = _rrf_score(signals_final, k=_RRF_K, weights=_RRF_WEIGHTS)

    results: list[RankedFunction] = []
    for i in range(n):
        results.append(
            RankedFunction(
                file=func_files[i],
                function=func_names[i],
                score=rrf_final[i],
                components={
                    "direct": score_direct[i],
                    "file": score_file[i],
                    "bm25": score_bm25[i],
                    "sem": score_sem[i],
                    "caller_prox": score_prox[i],
                    "callee_prop": score_callee_prop[i],
                    "rrf_initial": rrf_initial[i],
                },
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:_TOP_FUNCTIONS]


def rank(
    query: QueryObject,
    repo_path: str,
    graph_db_path: str,
) -> RankedResults:
    files = rank_files(query, repo_path, graph_db_path)
    functions = rank_functions(query, files, repo_path, graph_db_path)
    return RankedResults(files=files, functions=functions)
