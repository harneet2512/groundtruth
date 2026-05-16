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

import time
import threading
from dataclasses import dataclass, asdict
from typing import Any, Literal

from groundtruth.pretask.anchor_select import AnchorRecord, select_anchors
from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.graph_reach import compute_reach, graph_expand_candidates
from groundtruth.pretask.anchor_proximity import compute_anchor_proximity
from groundtruth.pretask.hub_penalty import compute_hub_penalties, W_HUB_MAX
from groundtruth.pretask.hybrid import lexical_file_search

Ablation = Literal["A", "B0", "B1", "C", "D"]

# Default coefficients (calibrated on held-out calibration subset in step 2d)
# W_LEX is the BM25 weight — kept separate from W_SEM (dense cosine) so each
# signal is independently weighted rather than collapsed via max-fusion.
DEFAULT_WEIGHTS: dict[str, float] = {
    "W_SEM": 0.25,
    "W_LEX": 0.35,
    "W_REACH": 0.2,
    "W_PROX": 0.05,
    "W_HUB": 0.15,
    "W_COMMIT": 0.0,
}

DEFAULT_K_ANCHOR = 5
DEFAULT_K_SEM_TOP = 20
DEFAULT_TAU_ANCHOR = 0.30
DEFAULT_MAX_DEPTH = 3
DEFAULT_FOCUS_SIZE = 3  # hard cap on focus set — never grows above this
DEFAULT_MAX_GRAPH_EXPAND = 20  # cap on graph-expanded candidates (top-N by reach score)


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
    )
    w_hub = min(W_HUB_MAX, weights.get("W_HUB", 0))
    hub_sub = w_hub * hub_pen if evidence_pre_hub < w_hub else 0.0
    return evidence_pre_hub - hub_sub


def _ablation_weights(ablation: Ablation, base_weights: dict[str, float]) -> dict[str, float]:
    if ablation == "A":
        # Dense similarity only: no BM25, no graph, no hub
        return {**base_weights, "W_LEX": 0.0, "W_REACH": 0.0, "W_PROX": 0.0, "W_HUB": 0.0, "W_COMMIT": 0.0}
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

    # Stage A candidate set = semantic top-K ∪ graph-expanded
    sem_files = set(sem_scores.keys())
    candidate_set = sem_files | graph_expanded
    all_files = list(candidate_set)

    # Lexical scores: normalized BM25 kept as a separate component (W_LEX weight).
    # Separating BM25 from dense cosine (W_SEM) prevents a BM25-rank-1 file from
    # receiving sem=1.0 via max-fusion and overriding gold files with cosine=0.87-0.92.
    # BM25-only files are bounded by W_LEX * 1.0 instead of W_SEM * 1.0, and since
    # calibration drives W_LEX < W_SEM, high-cosine gold files retain their ranking.
    # This is the standard hybrid retrieval formulation (Ma et al. 2022, BEIR papers).
    lex_scores: dict[str, float] = {}
    _lex_hits = lexical_file_search(
        issue_text, repo_root, graph_db, IssueAnchors(),
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

    # Rank all candidates
    scored = [
        (fp, _total_score(components_map[fp], effective_weights), components_map[fp])
        for fp in all_files
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

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
