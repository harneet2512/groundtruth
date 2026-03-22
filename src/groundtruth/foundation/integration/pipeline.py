"""Staged retrieval pipeline — similarity → graph expansion → freshness → validation.

This is the main integration point for Foundation v2. It combines multi-signal
similarity, graph expansion, and freshness checking into a single query path
that produces obligation candidates.

All behavior is gated by GT_ENABLE_FOUNDATION. When the flag is OFF, this module
is never called and GT behaves identically to before.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from groundtruth.foundation.graph.expander import ExpandedNode, GraphExpander
from groundtruth.foundation.graph.rules import ALL_RULES
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.foundation.similarity.composite import find_related
from groundtruth.validators.obligations import Obligation


@dataclass
class PipelineResult:
    """Result of the staged retrieval pipeline."""

    # Similarity-sourced obligation candidates
    candidates: list[Obligation] = field(default_factory=list)

    # Process metrics for evaluation
    similarity_candidates: int = 0
    graph_expanded: int = 0
    freshness_filtered: int = 0
    validation_passed: int = 0
    latency_ms: float = 0.0

    # Evidence for debugging
    evidence: list[dict[str, object]] = field(default_factory=list)


@dataclass
class SimilarityCandidate:
    """Internal: a candidate from similarity + graph expansion before validation."""

    symbol_id: int
    score: float
    source: str  # 'similarity' | 'graph_expansion'
    relation: str | None  # edge_type for graph-sourced
    evidence: dict[str, object] = field(default_factory=dict)


def run_pipeline(
    symbol_id: int,
    symbol_name: str,
    file_path: str,
    repr_store: RepresentationStore,
    graph_expander: GraphExpander,
    stale_files: set[str] | None = None,
    use_case: str = "obligation_expansion",
    max_candidates: int = 10,
) -> PipelineResult:
    """Run the full staged retrieval pipeline for a symbol.

    Stages:
    1. Multi-signal similarity: find structurally related symbols
    2. Graph expansion: expand through callers/callees/class/overrides
    3. Freshness check: flag stale candidates
    4. Validation: filter to real obligation candidates

    Returns PipelineResult with candidates and process metrics.
    """
    start = time.time()
    result = PipelineResult()
    all_candidates: list[SimilarityCandidate] = []

    # ---- Stage 1: Multi-signal similarity ----
    try:
        related = find_related(
            store=repr_store,
            symbol_id=symbol_id,
            use_case=use_case,
            top_k=max_candidates * 2,  # get more than needed for filtering
        )
        for cand_id, score, evidence in related:
            all_candidates.append(SimilarityCandidate(
                symbol_id=cand_id,
                score=score,
                source="similarity",
                relation=None,
                evidence=evidence,
            ))
        result.similarity_candidates = len(related)
    except Exception:
        pass  # Similarity failure is non-fatal

    # ---- Stage 2: Graph expansion ----
    try:
        seed_ids = [symbol_id] + [c.symbol_id for c in all_candidates[:5]]
        expanded: list[ExpandedNode] = graph_expander.expand(
            seed_ids=seed_ids,
            expansion_rules=ALL_RULES,
            max_depth=1,
            max_expanded=20,
        )
        existing_ids = {c.symbol_id for c in all_candidates} | {symbol_id}
        for node in expanded:
            if node.symbol_id not in existing_ids:
                all_candidates.append(SimilarityCandidate(
                    symbol_id=node.symbol_id,
                    score=0.5 * (1.0 / (node.depth + 1)),  # decay by depth
                    source="graph_expansion",
                    relation=node.relation,
                    evidence={"edge_type": node.relation, "depth": node.depth},
                ))
                existing_ids.add(node.symbol_id)
        result.graph_expanded = len(expanded)
    except Exception:
        pass  # Graph expansion failure is non-fatal

    # ---- Stage 3: Freshness filter ----
    if stale_files:
        filtered = []
        for cand in all_candidates:
            meta = repr_store.get_metadata(cand.symbol_id)
            if meta and meta.file_path in stale_files:
                result.freshness_filtered += 1
                cand.evidence["stale"] = True
            filtered.append(cand)
        all_candidates = filtered

    # ---- Stage 4: Validation → Obligation candidates ----
    # Only candidates with sufficient evidence become obligations
    for cand in sorted(all_candidates, key=lambda c: c.score, reverse=True):
        if len(result.candidates) >= max_candidates:
            break

        # Resolve symbol name from metadata
        meta = repr_store.get_metadata(cand.symbol_id)
        if meta is None:
            continue

        # Skip stale candidates (suppressed, not removed)
        if cand.evidence.get("stale"):
            continue

        # Determine confidence based on source and score
        if cand.source == "similarity" and cand.score >= 0.8:
            confidence = min(0.7, cand.score * 0.8)  # cap below attribute-traced
        elif cand.source == "graph_expansion":
            confidence = min(0.5, cand.score)
        else:
            confidence = min(0.4, cand.score * 0.5)

        # Build reason string
        if cand.source == "similarity":
            reason = f"structurally similar (score={cand.score:.2f})"
        else:
            reason = f"graph-connected via {cand.relation} (depth={cand.evidence.get('depth', '?')})"

        obligation = Obligation(
            kind="similarity_sourced",
            source=symbol_name,
            target=f"symbol_id:{cand.symbol_id}",
            target_file=meta.file_path,
            target_line=None,
            reason=f"[foundation] {reason}",
            confidence=confidence,
        )
        result.candidates.append(obligation)
        result.evidence.append({
            "symbol_id": cand.symbol_id,
            "source": cand.source,
            "score": cand.score,
            **cand.evidence,
        })

    result.validation_passed = len(result.candidates)
    result.latency_ms = (time.time() - start) * 1000
    return result


def enhance_obligations(
    existing_obligations: list[Obligation],
    symbol_name: str,
    symbol_id: int | None,
    file_path: str,
    repr_store: RepresentationStore | None,
    graph_expander: GraphExpander | None,
    stale_files: set[str] | None = None,
) -> list[Obligation]:
    """Enhance existing obligations with foundation-sourced candidates.

    This is the primary integration function called by the obligation engine.
    Foundation-sourced obligations are ADDITIVE — they never replace or modify
    existing attribute-traced obligations.

    Returns the original obligations + any qualifying foundation candidates,
    deduplicated by target.
    """
    if repr_store is None or graph_expander is None or symbol_id is None:
        return existing_obligations

    pipeline_result = run_pipeline(
        symbol_id=symbol_id,
        symbol_name=symbol_name,
        file_path=file_path,
        repr_store=repr_store,
        graph_expander=graph_expander,
        stale_files=stale_files,
    )

    # Deduplicate: don't add foundation candidates that overlap with existing
    existing_targets = {(o.target, o.target_file) for o in existing_obligations}
    new_obligations = list(existing_obligations)

    for candidate in pipeline_result.candidates:
        if (candidate.target, candidate.target_file) not in existing_targets:
            new_obligations.append(candidate)

    return new_obligations
