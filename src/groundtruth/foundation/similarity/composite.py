"""Composite similarity query — weighted multi-representation scoring.

Combines fingerprint, astvec, and tokensketch distances with use-case-specific
weight profiles to find related symbols.
"""

from __future__ import annotations

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


def find_related(
    store: RepresentationStore,
    symbol_id: int,
    use_case: str,
    top_k: int = 10,
    scope: str | None = None,
    scope_value: str | None = None,
) -> list[tuple[int, float, dict[str, object]]]:
    """Find related symbols using weighted multi-representation scoring.

    Args:
        store: RepresentationStore to query.
        symbol_id: The query symbol's ID.
        use_case: One of 'rename_move', 'obligation_expansion',
                  'convention_cluster', 'test_matching'.
        top_k: Maximum results to return.
        scope: Optional scope filter: 'same_class', 'same_module', 'same_package'.
        scope_value: Value for the scope filter.

    Returns:
        List of (symbol_id, similarity_score, evidence_dict) tuples,
        sorted by score descending. Score is 0.0 (maximally different) to 1.0 (identical).
    """
    profile = USE_CASE_PROFILES.get(use_case)
    if profile is None:
        return []

    weights: dict[str, float] = profile["weights"]  # type: ignore[assignment]
    threshold: float = profile["threshold"]  # type: ignore[assignment]

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

    # For each rep_type, get all stored representations and compute distances
    # Accumulate per-candidate scores
    candidate_scores: dict[int, float] = {}
    candidate_evidence: dict[int, dict[str, object]] = {}
    active_weight_sum = 0.0

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

    if active_weight_sum == 0.0:
        return []

    # Normalize scores by active weight sum
    results: list[tuple[int, float, dict[str, object]]] = []
    for cand_id, raw_score in candidate_scores.items():
        normalized_score = raw_score / active_weight_sum
        if normalized_score >= threshold:
            evidence = candidate_evidence[cand_id]
            evidence["use_case"] = use_case
            evidence["composite_score"] = round(normalized_score, 4)
            results.append((cand_id, round(normalized_score, 4), evidence))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]
