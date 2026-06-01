"""v7.4 brief — semantic-anchored multi-hop localization reranker.

Two stages:
  Stage A — candidate generation: semantic_top_K ∪ graph_expand(trusted_anchors)
  Stage B — reranking: hybrid score (sem + lex + reach + anchor_prox - hub_pen)

Score components (independent weights, calibrated on 20-bug split):
  sem  — dense cosine similarity (sentence-transformer)
  lex  — normalized BM25 score (lexical overlap with issue text)
  reach — graph BFS reachability from trusted anchors (hub-scaled)
  anchor_prox — proximity to trusted anchors in call graph
  hub_pen — hub penalty: tanh(in_degree / HUB_SCALE)

Ablation variants (controlled by 'ablation' parameter):
  A  — dense only (W_SEM; W_LEX=W_REACH=W_PROX=W_HUB=W_COMMIT=0)
  B0 — graph only, symbol-match anchors only (W_SEM=W_LEX=0)
  B1 — graph rerank from semantic anchors (W_SEM=W_LEX=0)
  C  — hybrid core (all terms; W_COMMIT=0)
  D  — hybrid + commit prior (C + W_COMMIT > 0)

Feature-flag: GT_BRIEF_VERSION=v7_4 activates this scorer.
"""
from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Literal

from groundtruth.pretask.anchor_select import AnchorRecord, select_anchors
from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.graph_reach import compute_reach, graph_expand_candidates
from groundtruth.pretask.anchor_proximity import compute_anchor_proximity
from groundtruth.pretask.hub_penalty import compute_hub_penalties, W_HUB_MAX
from groundtruth.pretask.hybrid import lexical_file_search
from groundtruth.pretask.traces import parse_stack_traces

Ablation = Literal["A", "B0", "B1", "C", "D"]

# Default coefficients (calibrated on held-out calibration subset in step 2d)
# W_LEX is the BM25 weight — kept separate from W_SEM (dense cosine) so each
# signal is independently weighted rather than collapsed via max-fusion.
DEFAULT_WEIGHTS: dict[str, float] = {
    "W_SEM": 0.15,
    "W_LEX": 0.50,
    "W_REACH": 0.05,
    "W_PROX": 0.05,
    "W_HUB": 0.10,
    "W_COMMIT": 0.0,
    "W_PATH": 0.45,
    # W_FRAME — weight on the "file the runtime says failed" signal: a file named
    # in a parsed stack-trace frame (traces.py) or typed verbatim as a path in the
    # issue (anchors.IssueAnchors.paths), resolved to an indexed graph file_path.
    # Set ~0.6 — above W_PATH so an explicit-failure file beats a mere keyword-in-
    # basename prior, mirroring stack_frame_hits (hybrid.py:392) / direct_path_hits
    # (hybrid.py:384). The component is 0 when no frame/path resolves, so tasks with
    # no traceback (e.g. a [question] issue) degrade EXACTLY to the pre-change ranker
    # — the critical no-regression property (correct-or-quiet).
    "W_FRAME": 0.60,
    # W_CODE_DEF — definition-site signal for backtick-wrapped code symbols. The
    # reporter wrote `request.trusted_hosts` explicitly → resolve to definition
    # file via graph.db nodes table (deterministic, $0, same as LSP definition).
    # Weight above W_FRAME: an explicitly-coded symbol reference is the strongest
    # localization signal short of a direct file path. Research: ORACLE-SWE 2026
    # (definition-site = gold standard), SweRank ICLR 2025 (code entity resolution).
    "W_CODE_DEF": 0.70,
}

DEFAULT_K_ANCHOR = 5
DEFAULT_K_SEM_TOP = 20
DEFAULT_TAU_ANCHOR = 0.30
DEFAULT_MAX_DEPTH = 3
DEFAULT_FOCUS_SIZE = 3  # hard cap on focus set — never grows above this
DEFAULT_MAX_GRAPH_EXPAND = 20  # cap on graph-expanded candidates (top-N by reach score)

_DOCS_EXTENSIONS = frozenset({".md", ".rst", ".txt"})
_DOCS_FILENAMES = frozenset({
    "readme", "changelog", "changes", "contributing", "license", "authors",
    "history", "news", "todo", "acknowledgments",
})
_SOURCE_PREFIXES = ("src/", "lib/", "pkg/", "internal/", "core/", "app/")


def _is_docs_file(path_lower: str) -> bool:
    """Check if a file path is a documentation file (not a fix target)."""
    base = os.path.basename(path_lower)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    ext = "." + base.rsplit(".", 1)[1] if "." in base else ""
    if ext in _DOCS_EXTENSIONS:
        return True
    if stem in _DOCS_FILENAMES:
        return True
    if any(path_lower.startswith(d) for d in ("docs/", "doc/", "documentation/")):
        return True
    return False


def _is_source_dir(path_lower: str) -> bool:
    """Check if a file is in a typical source directory."""
    return any(path_lower.startswith(p) for p in _SOURCE_PREFIXES)


@dataclass
class RankedFile:
    rank: int
    path: str
    score: float
    components: dict[str, float]
    entered_via: str  # "semantic_seed" | "graph_rescue" | "both"
    min_path_length_from_anchor: int
    is_gold: bool = False


@dataclass
class V74BriefResult:
    bug_id: str
    repo: str
    hyperparameters: dict[str, Any]
    anchors: list[dict]
    anchor_trust: list[dict]
    candidate_set_size: int
    ranked_top10_focus: list[dict]
    ranked_full: list[dict]
    focus_set: list[str]
    focus_set_size: int
    gold_files: list[str]
    gold_in_focus: bool
    first_gold_rank_focus: int | None
    first_gold_rank_full: int | None
    ablation_variant: str
    elapsed_ms: int = 0


_CACHED_MODEL: Any = None
_MODEL_LOCK = threading.Lock()
_SEMANTIC_AVAILABLE: bool | None = None  # None = not yet probed


class _ZeroEmbeddingModel:
    """Fallback model that returns zero embeddings when sentence-transformers is unavailable.

    All semantic scores become 0.0, so BM25 (W_LEX) and graph signals drive ranking alone.
    """

    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
        batch_size: int = 128,
    ) -> Any:
        try:
            import numpy as _np
            return _np.zeros((len(texts), 384), dtype=_np.float32)
        except ImportError:
            return [[0.0] * 384 for _ in texts]


def _get_model() -> Any:
    """Lazy-load sentence-transformers model (cached per process, thread-safe).

    If sentence-transformers is not installed, returns a _ZeroEmbeddingModel
    that produces zero vectors.  This makes the semantic score 0 for all
    candidates while BM25 (W_LEX=0.35) and graph signals still work.
    """
    global _CACHED_MODEL, _SEMANTIC_AVAILABLE
    with _MODEL_LOCK:
        if _CACHED_MODEL is None:
            try:
                from sentence_transformers import SentenceTransformer
                _CACHED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
                _SEMANTIC_AVAILABLE = True
            except (ImportError, Exception) as exc:
                import logging
                logging.getLogger("groundtruth.pretask.v7_4_brief").warning(
                    "sentence-transformers unavailable (%s); semantic scores will be 0. "
                    "BM25 + graph signals will drive ranking.",
                    exc,
                )
                _CACHED_MODEL = _ZeroEmbeddingModel()
                _SEMANTIC_AVAILABLE = False
    return _CACHED_MODEL


def _score_variant_A(
    sem_scores: dict[str, float],
    lex_scores: dict[str, float],
    all_files: list[str],
) -> dict[str, dict[str, float]]:
    """Variant A: dense similarity only (no BM25, no graph)."""
    return {
        fp: {
            "sem": sem_scores.get(fp, 0.0),
            "lex": 0.0,
            "reach": 0.0,
            "anchor_prox": 0.0,
            "hub_pen": 0.0,
            "commit": 0.0,
        }
        for fp in all_files
    }


def _score_variant_B(
    reach_scores: dict[str, Any],
    anchor_prox: dict[str, float],
    all_files: list[str],
    sem_scores: dict[str, float],
    lex_scores: dict[str, float],
    *,
    use_semantic_seed: bool,  # B0=False, B1=True
) -> dict[str, dict[str, float]]:
    """Variants B0/B1: graph-only (W_SEM=W_LEX=0 via ablation weights)."""
    result = {}
    for fp in all_files:
        r = reach_scores.get(fp)
        result[fp] = {
            "sem": sem_scores.get(fp, 0.0) if use_semantic_seed else 0.0,
            "lex": lex_scores.get(fp, 0.0),
            "reach": r.reach_score if r else 0.0,
            "anchor_prox": anchor_prox.get(fp, 0.0),
            "hub_pen": 0.0,
            "commit": 0.0,
        }
    return result


def _score_variant_C(
    sem_scores: dict[str, float],
    lex_scores: dict[str, float],
    reach_scores: dict[str, Any],
    anchor_prox: dict[str, float],
    hub_penalties: dict[str, float],
    all_files: list[str],
    commit_scores: dict[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Variants C/D: full hybrid (dense + lexical + graph)."""
    result = {}
    for fp in all_files:
        r = reach_scores.get(fp)
        result[fp] = {
            "sem": sem_scores.get(fp, 0.0),
            "lex": lex_scores.get(fp, 0.0),
            "reach": r.reach_score if r else 0.0,
            "anchor_prox": anchor_prox.get(fp, 0.0),
            "hub_pen": hub_penalties.get(fp, 0.0),
            "commit": commit_scores.get(fp, 0.0) if commit_scores else 0.0,
        }
    return result


def _total_score(components: dict[str, float], weights: dict[str, float]) -> float:
    hub_pen = components.get("hub_pen", 0.0)
    reach_contrib = weights.get("W_REACH", 0) * components.get("reach", 0.0) * max(0.0, 1.0 - hub_pen)
    evidence_pre_hub = (
        weights.get("W_SEM", 0) * components.get("sem", 0.0)
        + weights.get("W_LEX", 0) * components.get("lex", 0.0)
        + reach_contrib
        + weights.get("W_PROX", 0) * components.get("anchor_prox", 0.0)
        + weights.get("W_COMMIT", 0) * components.get("commit", 0.0)
        + weights.get("W_PATH", 0) * components.get("path", 0.0)
        # frame: explicit-failure signal (stack-trace frame / typed path). Absent
        # key -> 0.0 -> exact no-op when no traceback/path resolves. Same additive
        # form as W_PATH so a resolvable failure file ranks above keyword-only files.
        + weights.get("W_FRAME", 0) * components.get("frame", 0.0)
        + weights.get("W_CODE_DEF", 0) * components.get("code_def", 0.0)
    )
    w_hub = min(W_HUB_MAX, weights.get("W_HUB", 0))
    # Degree-normalized hub penalty applies to EVERY hub, not only near-zero-
    # evidence files. The prior gate (`if evidence_pre_hub < w_hub`) zeroed the
    # penalty for exactly the well-evidenced hubs that out-rank specific modules
    # (the B4 mislocalization): measured 59% of hub candidates silently un-
    # penalized on a real graph, with the penalty firing only on hubs that had no
    # keyword evidence (already ranked last) — an inversion. A high-in-degree hub
    # that matched issue keywords is the dangerous false positive; the degree-
    # normalization penalty (DOC_OF_HONOR Layer 0.5) must bite it.
    #
    # hub_pen==0 (non-hub) -> hub_sub==0 -> exact no-op (the no-regression
    # property). w_hub is small (0.1) by design: a tie-breaker that flips close
    # hub-vs-specific contests, never a sledgehammer — a hub whose evidence beats
    # a rival by > w_hub still wins (a legitimately-relevant hub stays top).
    # Floored at 0 so a pure hub ranks last rather than going negative.
    hub_sub = w_hub * hub_pen
    return max(0.0, evidence_pre_hub - hub_sub)


# --- RRF fusion (Cormack SIGIR 2009; SpIDER arXiv 2512.16956, 2025) ----------
# Rank-based, scale-invariant fusion of per-signal rankings. Research shows RRF
# beats a hand-weighted linear sum of incommensurate scores (BM25 vs cosine vs
# graph reach) WITHOUT learned weights, because it never lets one signal's raw
# scale dominate. GT_RRF_FUSION selects it; "det" drops the embedding signal
# (sem) for a fully deterministic, no-embeddings prior (our holdout data: the
# generic sentence-transformer adds ~2-3pp, so "det" is near-free + on-thesis).
_RRF_SIGNALS_FULL = ("sem", "lex", "reach", "anchor_prox", "path", "frame", "code_def")
_RRF_SIGNALS_DET = ("lex", "reach", "anchor_prox", "path", "frame", "code_def")


def _rrf_fuse(
    components_map: dict[str, dict[str, float]],
    files: list[str],
    signals: tuple[str, ...],
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion over per-signal rankings.

    For each signal, rank the files with a POSITIVE value for it; each such file
    gains 1/(k + rank). A file with a zero/absent value for a signal gets nothing
    from it (treated as unranked), so a single strong signal cannot dominate the
    way it does in a weighted sum. k=60 is the SIGIR-2009 convention.
    """
    agg: dict[str, float] = {fp: 0.0 for fp in files}
    for sig in signals:
        ranked = sorted(
            (fp for fp in files if components_map.get(fp, {}).get(sig, 0.0) > 0.0),
            key=lambda fp: components_map[fp].get(sig, 0.0),
            reverse=True,
        )
        for rank, fp in enumerate(ranked, start=1):
            agg[fp] += 1.0 / (k + rank)
    return agg


def _ablation_weights(ablation: Ablation, base_weights: dict[str, float]) -> dict[str, float]:
    if ablation == "A":
        # Dense similarity only: no BM25, no graph, no hub, no frame/path signal
        return {**base_weights, "W_LEX": 0.0, "W_REACH": 0.0, "W_PROX": 0.0, "W_HUB": 0.0, "W_COMMIT": 0.0, "W_FRAME": 0.0}
    if ablation == "B0":
        # Graph only (symbol-match anchors): no dense, no BM25
        return {**base_weights, "W_SEM": 0.0, "W_LEX": 0.0, "W_HUB": 0.0, "W_COMMIT": 0.0}
    if ablation == "B1":
        # Graph only (semantic anchors): no dense, no BM25
        return {**base_weights, "W_SEM": 0.0, "W_LEX": 0.0, "W_HUB": 0.0, "W_COMMIT": 0.0}
    if ablation == "C":
        return {**base_weights, "W_COMMIT": 0.0}
    # D: use all weights as-is
    return dict(base_weights)


def _resolve_against_graph_files(
    raw_path: str,
    graph_files: list[str],
    graph_basenames: dict[str, list[str]],
) -> str | None:
    """Resolve a raw trace/issue path to an indexed graph ``file_path``.

    Generalized suffix/basename match (no repo-specific logic):

      1. Suffix match — a graph file_path ends with the (normalized) raw path,
         or the raw path ends with the graph file_path. This handles the common
         case where the trace prints an absolute or longer path
         (``/build/src/foo/bar.py``) while the graph stores a repo-relative one
         (``src/foo/bar.py``), and vice versa.
      2. Unique-basename match — fall back to the bare filename only when exactly
         ONE indexed file has that basename (so an ambiguous ``utils.py`` that
         exists in five packages never resolves to the wrong one — correct-or-
         quiet: no resolution rather than a guess).

    Returns the matched graph file_path, or None when nothing resolves.
    """
    if not raw_path or not graph_files:
        return None
    norm = raw_path.replace("\\", "/").lstrip("./").lstrip("/")
    if not norm:
        return None

    # 1. Suffix match (longest graph path wins — most specific).
    suffix_matches = [
        gf for gf in graph_files
        if gf == norm
        or gf.endswith("/" + norm)
        or norm.endswith("/" + gf)
    ]
    if suffix_matches:
        return max(suffix_matches, key=len)

    # 2. Unique-basename fallback.
    base = os.path.basename(norm)
    candidates = graph_basenames.get(base, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _compute_frame_scores(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    issue_anchors: IssueAnchors,
) -> dict[str, float]:
    """Score indexed files by the explicit-failure signal, depth-decayed.

    Combines two correct-or-quiet sources, each resolved to an indexed
    ``file_path`` (suffix/unique-basename match):

      * Parsed stack-trace frames (traces.parse_stack_traces, deepest-first).
        The deepest in-repo frame is what the *runtime* says failed; arxiv
        2412.03905 reports 98.3% bug-location correlation for the deepest
        in-repo frame. We decay with frame DEPTH so the deepest frame scores
        ~1.0 and shallower frames score less — mirroring the rank decay in
        ``stack_frame_hits`` (hybrid.py:392): ``1/(idx+1)``.
      * Explicit path mentions the reporter typed verbatim (IssueAnchors.paths).
        Treated as a top-strength signal (1.0) — a path the human deliberately
        wrote in the issue body — mirroring ``direct_path_hits`` (hybrid.py:384).

    The returned map contains ONLY files that actually resolve to an indexed
    file. If there is no traceback and no resolvable path, the map is EMPTY, so
    the frame component is 0 for every candidate and the ranker degrades exactly
    to its pre-change behavior (the no-regression property).
    """
    scores: dict[str, float] = {}
    try:
        graph_files = graph_file_paths_for_frame(graph_db)
    except Exception:
        graph_files = []
    if not graph_files:
        return scores

    graph_basenames: dict[str, list[str]] = {}
    for gf in graph_files:
        graph_basenames.setdefault(os.path.basename(gf), []).append(gf)

    # Stack-trace frames, deepest-first. Depth decay 1/(idx+1): idx=0 (deepest in
    # the returned list) -> 1.0, idx=1 -> 0.5, ... (same form as hybrid.py:392/401).
    try:
        frames = parse_stack_traces(issue_text, repo_root)
    except Exception:
        frames = []
    for idx, fr in enumerate(frames):
        resolved = _resolve_against_graph_files(fr.file, graph_files, graph_basenames)
        if resolved is None:
            continue
        s = 1.0 / (idx + 1)
        if s > scores.get(resolved, 0.0):
            scores[resolved] = s

    # Verbatim path mentions: reporter-typed paths are a deliberate, high-precision
    # signal -> full strength (1.0), like direct_path_hits (hybrid.py:384).
    for raw in sorted(issue_anchors.paths):
        resolved = _resolve_against_graph_files(raw, graph_files, graph_basenames)
        if resolved is None:
            continue
        if 1.0 > scores.get(resolved, 0.0):
            scores[resolved] = 1.0

    return scores


def _compute_code_symbol_scores(
    issue_anchors: IssueAnchors,
    graph_db: str,
) -> dict[str, float]:
    """Score files by the CODE-SYMBOL definition-site signal.

    Backtick-wrapped symbols (``code_symbols``) are the highest-confidence issue
    anchors — the reporter explicitly marked them as code references. For each
    code_symbol, look up its definition file(s) in graph.db ``nodes`` table.
    Definition-site files score 1.0 (they contain the symbol the reporter named);
    multiple definitions split the score (1/n) to avoid hub amplification.

    This is the **graph-based equivalent of LSP textDocument/definition** — it
    resolves `` `request.trusted_hosts` `` to ``wrappers.py`` (where Request is
    defined) without a running LSP server. When runtime LSP is available, it can
    refine these results; but the graph-based version is deterministic, $0, and
    available at index time.

    Research: ORACLE-SWE 2026 (definition-site as gold standard for edit-location);
    SweRank ICLR 2025 (code entity resolution for localization). The signal is 0.0
    when no code_symbols exist or none resolve — exact no-op fallback.
    """
    scores: dict[str, float] = {}
    code_syms = getattr(issue_anchors, "code_symbols", set())
    if not code_syms or not graph_db:
        return scores
    try:
        import sqlite3 as _sql
        conn = _sql.connect(graph_db)
        for sym in code_syms:
            # Look up the last component (e.g., "trusted_hosts" from "request.trusted_hosts")
            parts = sym.split(".")
            lookup_name = parts[-1] if parts else sym
            if len(lookup_name) < 3:
                continue
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM nodes "
                "WHERE name = ? AND is_test = 0 AND file_path IS NOT NULL",
                (lookup_name,),
            ).fetchall()
            if not rows:
                continue
            # Score inversely proportional to ambiguity (1/n) — a unique definition
            # is a strong signal; a name defined in 10 files is weaker.
            weight = 1.0 / len(rows)
            for (fp,) in rows:
                norm = fp.replace("\\", "/").lstrip("./").lstrip("/")
                scores[norm] = max(scores.get(norm, 0.0), weight)
        conn.close()
    except Exception:
        pass
    return scores


def graph_file_paths_for_frame(graph_db: str) -> list[str]:
    """Distinct non-test indexed file_paths (normalized to forward slashes)."""
    if not graph_db:
        return []
    import sqlite3 as _sql
    conn = _sql.connect(graph_db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM nodes "
            "WHERE file_path IS NOT NULL AND is_test = 0"
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]).replace("\\", "/").lstrip("./").lstrip("/") for r in rows if r and r[0]]


def run_v74(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    *,
    bug_id: str = "unknown",
    repo: str = "unknown",
    gold_files: list[str] | None = None,
    ablation: Ablation = "C",
    k_anchor: int = DEFAULT_K_ANCHOR,
    k_sem_top: int = DEFAULT_K_SEM_TOP,
    k_lex_top: int = 10,
    tau_anchor: float = DEFAULT_TAU_ANCHOR,
    max_depth: int = DEFAULT_MAX_DEPTH,
    min_confidence: float = 0.7,
    max_graph_expand: int = DEFAULT_MAX_GRAPH_EXPAND,
    weights: dict[str, float] | None = None,
    focus_size: int = DEFAULT_FOCUS_SIZE,
    commit_scores: dict[str, float] | None = None,
) -> V74BriefResult:
    """Run the v7.4 scorer for one bug.

    Returns a V74BriefResult with full debug artifact fields.
    """
    t0 = time.perf_counter()
    effective_weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    effective_weights = _ablation_weights(ablation, effective_weights)

    # Extract real issue anchors ONCE (symbols cross-checked against nodes.name,
    # plus verbatim path mentions and test names). Reused for (a) enriching the
    # BM25 query terms passed to lexical_file_search — previously fed an EMPTY
    # IssueAnchors() so symbol/path signal was dropped — and (b) seeding the
    # explicit-path component of the frame signal below. Degrades to no-op when
    # the issue has no resolvable symbols/paths (extract returns empty sets).
    issue_anchors = extract_issue_anchors(issue_text, graph_db)

    model = _get_model()

    # When sentence-transformers is unavailable, zero out the semantic weight
    # so BM25 (W_LEX) and graph signals drive ranking alone.
    if not _SEMANTIC_AVAILABLE:
        effective_weights["W_SEM"] = 0.0

    # Stage A: anchor selection
    anchors, sem_scores = select_anchors(
        issue_text, repo_root, graph_db, model,
        k_anchor=k_anchor,
        k_sem_top=k_sem_top,
        k_lex_top=k_lex_top,
        tau_anchor=tau_anchor,
    )

    trusted = [a.path for a in anchors if a.trusted_for_expansion]

    # For B0: only symbol-match anchors seed the graph
    if ablation == "B0":
        trusted = [a.path for a in anchors if a.reason in ("symbol_match", "both")]

    # Graph expansion
    if ablation == "A":
        graph_expanded: set[str] = set()
        reach_scores = {}
        prox_scores: dict[str, float] = {}
        hub_penalties: dict[str, float] = {}
    else:
        # v7.5 H2: compute hub penalties before BFS so reach accumulation can
        # discount paths through hub intermediate nodes (path-specificity weighting).
        # Only for hybrid variants (C/D); graph-only variants use unweighted BFS.
        if ablation in ("C", "D"):
            hub_penalties = compute_hub_penalties(graph_db)
        else:
            hub_penalties = {}

        graph_expanded = graph_expand_candidates(
            trusted, graph_db, max_depth=max_depth, min_confidence=min_confidence
        )
        reach_scores = compute_reach(
            trusted, graph_db,
            max_depth=max_depth,
            min_confidence=min_confidence,
            hub_penalties=hub_penalties,
        )
        prox_scores = compute_anchor_proximity(trusted, graph_db)

        # Cap graph-expanded set to top-N by reach score (prevents bloat on large repos).
        # Files already in the semantic top-K are excluded from this cap since they enter
        # via the semantic seed path, not graph rescue.
        sem_files_pre = set(sem_scores.keys())
        graph_only = graph_expanded - sem_files_pre
        if len(graph_only) > max_graph_expand:
            anchor_set_paths = set(trusted)
            by_reach = sorted(
                ((fp, reach_scores[fp].reach_score) for fp in graph_only if fp in reach_scores),
                key=lambda x: x[1],
                reverse=True,
            )
            graph_expanded = sem_files_pre | anchor_set_paths | {fp for fp, _ in by_reach[:max_graph_expand]}

    # Stage A candidate set = semantic top-K ∪ graph-expanded ∪ BM25 top-K ∪ path-matched
    sem_files = set(sem_scores.keys())
    candidate_set = sem_files | graph_expanded

    # Stage B: full-source BM25 recall — add top BM25 results to candidate set.
    # This ensures files findable by keyword content are always candidates,
    # not just files found by semantic similarity or graph expansion.
    _lex_candidates = lexical_file_search(
        issue_text, repo_root, graph_db, issue_anchors,
        max_files=max(20, len(candidate_set)),
    )
    _lex_top_paths = {h.file for h in (_lex_candidates or [])[:10]}
    candidate_set |= _lex_top_paths

    # Path/name rescue: add files whose path contains issue identifiers.
    # Bidirectional substring: "color" matches "_colorama", "balance" matches "balance".
    import re as _re_fn
    import sqlite3 as _sql_fn
    _issue_words_fn = set(w.lower() for w in _re_fn.findall(r"[A-Za-z_]\w{2,}", issue_text) if len(w) >= 4)
    try:
        _conn_fn = _sql_fn.connect(graph_db)
        _all_graph_files = [r[0] for r in _conn_fn.execute("SELECT DISTINCT file_path FROM nodes WHERE is_test = 0").fetchall()]
        _conn_fn.close()
        for fp in _all_graph_files:
            basename = os.path.basename(fp).rsplit(".", 1)[0].lower()
            for iw in _issue_words_fn:
                if iw in basename or basename in iw:
                    candidate_set.add(fp)
                    break
    except Exception:
        pass

    # Frame/path signal: resolve stack-trace frames + verbatim path mentions to
    # indexed file_paths (depth-decayed). Keyed by NORMALIZED path so it matches
    # candidates regardless of slash/prefix differences across indexers. A
    # frame-resolved file that no other signal surfaced is ADDED to the candidate
    # set — this is exactly the case the signal exists for (the runtime named the
    # failing file but semantic/BM25/path missed it). Empty when no traceback/path
    # resolves -> pure no-op (no candidates added, frame component 0 everywhere).
    if effective_weights.get("W_FRAME", 0.0) > 0.0:
        frame_scores = _compute_frame_scores(issue_text, repo_root, graph_db, issue_anchors)
    else:
        frame_scores = {}
    # Tier 2: code-symbol definition-site scores (backtick-wrapped symbols →
    # definition files via graph.db). The graph-based LSP-equivalent: resolves
    # `request.trusted_hosts` → wrappers.py without a running LSP server.
    if effective_weights.get("W_CODE_DEF", 0.0) > 0.0:
        code_def_scores = _compute_code_symbol_scores(issue_anchors, graph_db)
    else:
        code_def_scores = {}
    if code_def_scores:
        _existing_norm_cd = {
            fp.replace("\\", "/").lstrip("./").lstrip("/") for fp in candidate_set
        }
        for resolved_norm_cd in code_def_scores:
            if resolved_norm_cd not in _existing_norm_cd:
                candidate_set.add(resolved_norm_cd)
    if frame_scores:
        _existing_norm = {
            fp.replace("\\", "/").lstrip("./").lstrip("/") for fp in candidate_set
        }
        for resolved_norm in frame_scores:
            if resolved_norm not in _existing_norm:
                candidate_set.add(resolved_norm)

    all_files = list(candidate_set)

    # Lexical scores: normalized BM25 kept as a separate component (W_LEX weight).
    # Separating BM25 from dense cosine (W_SEM) prevents a BM25-rank-1 file from
    # receiving sem=1.0 via max-fusion and overriding gold files with cosine=0.87-0.92.
    # BM25-only files are bounded by W_LEX * 1.0 instead of W_SEM * 1.0, and since
    # calibration drives W_LEX < W_SEM, high-cosine gold files retain their ranking.
    # This is the standard hybrid retrieval formulation (Ma et al. 2022, BEIR papers).
    lex_scores: dict[str, float] = {}
    _lex_hits = lexical_file_search(
        issue_text, repo_root, graph_db, issue_anchors,
        max_files=max(50, len(all_files)),
    )
    if _lex_hits:
        _max_lex = max(h.score for h in _lex_hits)
        if _max_lex > 0:
            for h in _lex_hits:
                lex_scores[h.file] = h.score / _max_lex

    # Normalize reach scores to [0, 1] so the reach term is comparable to
    # the semantic term (which is cosine similarity, already in [0, 1]).
    # Without normalization, hub files reachable via many paths from many
    # anchors accumulate reach scores in the hundreds/thousands, completely
    # overwhelming W_SEM * sem (which is at most ~0.5).
    if reach_scores:
        max_reach = max((r.reach_score for r in reach_scores.values()), default=0.0)
        if max_reach > 0:
            from groundtruth.pretask.graph_reach import ReachRecord
            reach_scores = {
                fp: ReachRecord(
                    path=r.path,
                    reach_score=r.reach_score / max_reach,
                    min_path_length=r.min_path_length,
                    entered_via_graph=r.entered_via_graph,
                )
                for fp, r in reach_scores.items()
            }

    # Stage B: compute score components
    if ablation == "A":
        components_map = _score_variant_A(sem_scores, lex_scores, all_files)
    elif ablation in ("B0", "B1"):
        components_map = _score_variant_B(
            reach_scores, prox_scores, all_files, sem_scores, lex_scores,
            use_semantic_seed=(ablation == "B1"),
        )
    else:  # C or D — hub_penalties already computed above for path-specificity BFS
        components_map = _score_variant_C(
            sem_scores, lex_scores, reach_scores, prox_scores, hub_penalties, all_files,
            commit_scores,
        )

    # Path-name prior: boost files whose path/name matches issue terms.
    # Uses bidirectional substring: "color" in issue matches "colorama" in filename.
    import re as _re_path
    _issue_words = set(w.lower() for w in _re_path.findall(r"[A-Za-z_]\w{2,}", issue_text) if len(w) >= 4)
    path_scores: dict[str, float] = {}
    for fp in all_files:
        basename = os.path.basename(fp).rsplit(".", 1)[0].lower()
        score = 0.0
        for iw in _issue_words:
            if iw == basename:
                score = max(score, 1.0)
            elif iw in basename or basename in iw:
                score = max(score, 0.7)
            elif iw in basename.replace("_", ""):
                score = max(score, 0.5)
        # Directory matches
        for part in Path(fp).parts[:-1]:
            part_l = part.lower()
            if len(part_l) >= 4:
                for iw in _issue_words:
                    if iw in part_l or part_l in iw:
                        score = max(score, 0.4)
                        break
        if score > 0:
            path_scores[fp] = score

    # Inject path + frame + code_def scores into components. Keyed by normalized path.
    for fp in all_files:
        _fp_norm = fp.replace("\\", "/").lstrip("./").lstrip("/")
        _frame_val = frame_scores.get(_fp_norm, 0.0)
        _cdef_val = code_def_scores.get(_fp_norm, 0.0)
        if fp in components_map:
            components_map[fp]["path"] = path_scores.get(fp, 0.0)
            components_map[fp]["frame"] = _frame_val
            components_map[fp]["code_def"] = _cdef_val
        else:
            components_map[fp] = {"path": path_scores.get(fp, 0.0), "frame": _frame_val, "code_def": _cdef_val}

    # Rank all candidates. GT_RRF_FUSION replaces the hand-weighted linear sum
    # with rank-based reciprocal rank fusion (research #1 fusion lever). Default
    # unset -> legacy linear sum (exact no-regression). "det" drops embeddings.
    _rrf_mode = os.environ.get("GT_RRF_FUSION", "").strip().lower()
    if _rrf_mode in ("1", "on", "full", "rrf"):
        _rrf = _rrf_fuse(components_map, all_files, _RRF_SIGNALS_FULL)
        scored = [(fp, _rrf.get(fp, 0.0), components_map[fp]) for fp in all_files]
    elif _rrf_mode in ("det", "deterministic", "nosem"):
        _rrf = _rrf_fuse(components_map, all_files, _RRF_SIGNALS_DET)
        scored = [(fp, _rrf.get(fp, 0.0), components_map[fp]) for fp in all_files]
    else:
        scored = [
            (fp, _total_score(components_map[fp], effective_weights), components_map[fp])
            for fp in all_files
        ]

    # Docs/source ranking adjustment: penalize documentation files, boost source files.
    _docs_penalty = float(os.environ.get("GT_DOCS_PENALTY", "0.3"))
    _source_boost = float(os.environ.get("GT_SOURCE_BOOST", "1.1"))
    if _docs_penalty > 0 or _source_boost != 1.0:
        adjusted = []
        for fp, sc, comps in scored:
            fp_lower = fp.replace("\\", "/").lstrip("./").lower()
            if _is_docs_file(fp_lower):
                sc *= (1.0 - _docs_penalty)
            elif _is_source_dir(fp_lower) and _source_boost != 1.0:
                sc *= _source_boost
            adjusted.append((fp, sc, comps))
        scored = adjusted

    # Deterministic tie-break by path. The hub floor (max(0.0,...)) and equal
    # weak signals can tie many files at the same score; without a secondary key
    # their order falls back to list(candidate_set) = PYTHONHASHSEED (non-
    # reproducible). Sort by score desc, then path asc — fully deterministic.
    scored.sort(key=lambda x: (-x[1], x[0]))

    # Build ranked records
    gold_set = set(gold_files or [])
    ranked_records: list[RankedFile] = []
    for rank, (fp, score, comps) in enumerate(scored, start=1):
        r = reach_scores.get(fp)
        in_sem = fp in sem_files
        in_graph = fp in graph_expanded
        if in_sem and in_graph:
            entered_via = "both"
        elif in_graph:
            entered_via = "graph_rescue"
        else:
            entered_via = "semantic_seed"

        ranked_records.append(RankedFile(
            rank=rank,
            path=fp,
            score=round(score, 6),
            components={k: round(v, 6) for k, v in comps.items()},
            entered_via=entered_via,
            min_path_length_from_anchor=r.min_path_length if r else 999,
            is_gold=fp in gold_set,
        ))

    focus_set = [r.path for r in ranked_records[:focus_size]]
    gold_in_focus = bool(gold_set & set(focus_set))
    first_gold_rank_focus: int | None = None
    for r in ranked_records[:focus_size]:
        if r.is_gold:
            first_gold_rank_focus = r.rank
            break
    first_gold_rank_full: int | None = None
    for r in ranked_records:
        if r.is_gold:
            first_gold_rank_full = r.rank
            break

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Ranking diagnosis: log top-20 with component scores for observability
    _diag_path = os.environ.get("GT_DEBUG_DIR", "")
    if _diag_path and ranked_records:
        try:
            _diag_file = os.path.join(_diag_path, f"l1_ranking_diagnosis_{bug_id}.json")
            _lex_top20 = {h.file: h.score for h in (_lex_candidates or [])[:20]}
            _diag_data = {
                "bug_id": bug_id,
                "gold_files": list(gold_set),
                "candidate_set_size": len(all_files),
                "gold_in_candidate_set": bool(gold_set & set(all_files)),
                "gold_in_bm25_top20": bool(gold_set & set(_lex_top20.keys())),
                "gold_in_graph_expanded": bool(gold_set & graph_expanded),
                "gold_in_sem_files": bool(gold_set & sem_files),
                "first_gold_rank": first_gold_rank_full,
                "weights": effective_weights,
                "top_20": [
                    {
                        "rank": r.rank,
                        "path": r.path,
                        "score": r.score,
                        "components": r.components,
                        "entered_via": r.entered_via,
                        "is_gold": r.is_gold,
                        "bm25_raw": round(_lex_top20.get(r.path, 0.0), 4),
                        "path_score": round(path_scores.get(r.path, 0.0), 4),
                    }
                    for r in ranked_records[:20]
                ],
            }
            os.makedirs(_diag_path, exist_ok=True)
            with open(_diag_file, "w") as _df:
                json.dump(_diag_data, _df, indent=2)
        except Exception:
            pass

    hyperparameters = {
        "K_ANCHOR": k_anchor,
        "K_SEM_TOP": k_sem_top,
        "K_LEX_TOP": k_lex_top,
        "TAU_ANCHOR": tau_anchor,
        "max_depth": max_depth,
        "min_confidence": min_confidence,
        "max_graph_expand": max_graph_expand,
        **effective_weights,
    }

    return V74BriefResult(
        bug_id=bug_id,
        repo=repo,
        hyperparameters=hyperparameters,
        anchors=[{"path": a.path, "score": round(a.semantic_score, 4), "reason": a.reason}
                 for a in anchors],
        anchor_trust=[{"path": a.path, "trusted_for_expansion": a.trusted_for_expansion}
                      for a in anchors],
        candidate_set_size=len(all_files),
        ranked_top10_focus=[asdict(r) for r in ranked_records[:10]],
        ranked_full=[asdict(r) for r in ranked_records],
        focus_set=focus_set,
        focus_set_size=len(focus_set),
        gold_files=list(gold_files or []),
        gold_in_focus=gold_in_focus,
        first_gold_rank_focus=first_gold_rank_focus,
        first_gold_rank_full=first_gold_rank_full,
        ablation_variant=ablation,
        elapsed_ms=elapsed_ms,
    )
