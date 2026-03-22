"""Composite similarity query — weighted multi-representation scoring.

Combines fingerprint, astvec, and tokensketch distances with use-case-specific
weight profiles to find related symbols.

Includes anti-boilerplate measures:
- Prevalence penalty: common patterns (shared by many symbols) get penalized
- Token sketch requirement: high structural match without token overlap is suspect
"""

from __future__ import annotations

import math

from groundtruth.foundation.repr.registry import get_extractor
from groundtruth.foundation.repr.store import RepresentationStore

# Use-case scoring profiles: {rep_type: weight}, threshold
USE_CASE_PROFILES: dict[str, dict[str, float | dict[str, float]]] = {
    "rename_move": {
        "weights": {"fingerprint_v1": 0.7, "astvec_v1": 0.2, "tokensketch_v1": 0.1},
        "threshold": 0.9,
    },
    "obligation_expansion": {
        "weights": {"astvec_v1": 0.5, "tokensketch_v1": 0.3, "fingerprint_v1": 0.2},
        "threshold": 0.7,
    },
    "convention_cluster": {
        "weights": {"astvec_v1": 0.6, "tokensketch_v1": 0.3, "fingerprint_v1": 0.1},
        "threshold": 0.65,
    },
    "test_matching": {
        "weights": {"tokensketch_v1": 0.5, "astvec_v1": 0.3, "fingerprint_v1": 0.2},
        "threshold": 0.5,
    },
}

# Minimum token sketch similarity required for obligation_expansion.
# If two methods have identical structure but completely different tokens,
# they're likely boilerplate (different __init__, different getters), not coupled.
_MIN_TOKEN_SIMILARITY: dict[str, float] = {
    "obligation_expansion": 0.15,
    "convention_cluster": 0.0,   # conventions are about structure, tokens can differ
    "rename_move": 0.0,          # renames keep all tokens
    "test_matching": 0.0,
}


def _compute_prevalence_penalty(
    query_blob: bytes,
    all_blobs: list[tuple[int, bytes]],
    total_symbols: int,
) -> float:
    """IDF-style penalty for common fingerprints.

    If a fingerprint is shared by many symbols, it's likely boilerplate.
    Returns a multiplier in (0, 1]: rare patterns → ~1.0, ubiquitous → ~0.3.
    """
    if total_symbols <= 1:
        return 1.0
    # Count how many symbols share this exact fingerprint
    matches = sum(1 for _, blob in all_blobs if blob == query_blob)
    if matches <= 1:
        return 1.0
    # IDF-style: log(N / matches) / log(N), clamped to [0.3, 1.0]
    idf = math.log(total_symbols / matches) / math.log(total_symbols)
    return max(0.3, min(1.0, idf))


def find_related(
    store: RepresentationStore,
    symbol_id: int,
    use_case: str,
    top_k: int = 10,
    scope: str | None = None,
    scope_value: str | None = None,
) -> list[tuple[int, float, dict[str, object]]]:
    """Find related symbols using weighted multi-representation scoring.

    Anti-boilerplate measures:
    - Prevalence penalty: if query fingerprint is shared by many symbols,
      all scores for that query are penalized (common pattern = less informative)
    - Token gate: for obligation_expansion, candidates must have minimum token
      overlap (prevents linking structurally identical but lexically unrelated code)
    """
    profile = USE_CASE_PROFILES.get(use_case)
    if profile is None:
        return []

    weights: dict[str, float] = profile["weights"]  # type: ignore[assignment]
    threshold: float = profile["threshold"]  # type: ignore[assignment]
    min_token_sim = _MIN_TOKEN_SIMILARITY.get(use_case, 0.0)

    # Get the query symbol's representations
    query_reps: dict[str, bytes] = {}
    for rep_type in weights:
        rec = store.get_representation(symbol_id, rep_type)
        if rec is not None:
            query_reps[rep_type] = rec.rep_blob

    if not query_reps:
        return []

    # Determine which symbol IDs to compare against
    candidate_ids: set[int] | None = None

    if scope is not None and scope_value is not None:
        metadata_list = store.get_metadata_by_scope(scope, scope_value)
        candidate_ids = {m.symbol_id for m in metadata_list} - {symbol_id}
        if not candidate_ids:
            return []

    # Compute prevalence penalty from fingerprint distribution
    # Only for obligation_expansion — convention_cluster WANTS common patterns
    prevalence_penalty = 1.0
    fp_all_reps: list[tuple[int, bytes]] = []
    if use_case == "obligation_expansion" and "fingerprint_v1" in query_reps:
        fp_all_reps = store.get_all_representations("fingerprint_v1")
        total_symbols = len(fp_all_reps)
        prevalence_penalty = _compute_prevalence_penalty(
            query_reps["fingerprint_v1"], fp_all_reps, total_symbols,
        )

    # For each rep_type, get all stored representations and compute distances
    candidate_scores: dict[int, float] = {}
    candidate_evidence: dict[int, dict[str, object]] = {}
    active_weight_sum = 0.0

    # Cache token similarities for the token gate check
    candidate_token_sim: dict[int, float] = {}

    for rep_type, weight in weights.items():
        extractor = get_extractor(rep_type)
        if extractor is None or rep_type not in query_reps:
            continue

        active_weight_sum += weight
        all_reps = store.get_all_representations(rep_type)
        query_blob = query_reps[rep_type]

        for cand_id, cand_blob in all_reps:
            if cand_id == symbol_id:
                continue
            if candidate_ids is not None and cand_id not in candidate_ids:
                continue

            dist = extractor.distance(query_blob, cand_blob)
            similarity = 1.0 - dist

            if cand_id not in candidate_scores:
                candidate_scores[cand_id] = 0.0
                candidate_evidence[cand_id] = {}

            candidate_scores[cand_id] += similarity * weight
            candidate_evidence[cand_id][f"{rep_type}_similarity"] = round(similarity, 4)

            # Track token similarity for gate check
            if rep_type == "tokensketch_v1":
                candidate_token_sim[cand_id] = similarity

    if active_weight_sum == 0.0:
        return []

    # Normalize scores, apply prevalence penalty, apply token gate
    results: list[tuple[int, float, dict[str, object]]] = []
    for cand_id, raw_score in candidate_scores.items():
        normalized_score = raw_score / active_weight_sum

        # Apply prevalence penalty — common patterns get demoted
        penalized_score = normalized_score * prevalence_penalty

        # Token gate: reject candidates with high structural but no token overlap
        if min_token_sim > 0:
            tok_sim = candidate_token_sim.get(cand_id, 0.0)
            if tok_sim < min_token_sim:
                continue  # Skip — structurally similar but lexically unrelated

        if penalized_score >= threshold:
            evidence = candidate_evidence[cand_id]
            evidence["use_case"] = use_case
            evidence["composite_score"] = round(penalized_score, 4)
            if prevalence_penalty < 1.0:
                evidence["prevalence_penalty"] = round(prevalence_penalty, 4)
            results.append((cand_id, round(penalized_score, 4), evidence))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]
