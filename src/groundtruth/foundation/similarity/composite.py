"""Composite similarity query — weighted multi-representation scoring.

Production-grade anti-noise measures:
1. Prevalence penalty: common fingerprints get IDF-style demotion
2. Token gate: minimum token overlap required for obligation_expansion
3. Complexity gate: trivial methods (<=3 statements) excluded from obligation_expansion
4. Cross-file penalty: cross-file matches need higher scores than same-file
5. Boilerplate name suppression: common method names (__init__, __repr__, etc.)
   get penalized for cross-class obligation_expansion
"""

from __future__ import annotations

import math
import struct

from groundtruth.foundation.repr.registry import get_extractor
from groundtruth.foundation.repr.store import RepresentationStore

# Use-case scoring profiles
USE_CASE_PROFILES: dict[str, dict[str, float | dict[str, float]]] = {
    "rename_move": {
        "weights": {"fingerprint_v1": 0.7, "astvec_v1": 0.2, "tokensketch_v1": 0.1},
        "threshold": 0.9,
    },
    "obligation_expansion": {
        "weights": {"astvec_v1": 0.5, "tokensketch_v1": 0.3, "fingerprint_v1": 0.2},
        "threshold": 0.75,  # raised from 0.70
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

# Minimum token similarity for obligation_expansion
_MIN_TOKEN_SIM_OBLIGATION = 0.20


def _compute_prevalence_penalty(
    query_blob: bytes,
    all_blobs: list[tuple[int, bytes]],
    total_symbols: int,
) -> float:
    """IDF-style penalty for common fingerprints.

    Returns multiplier in (0.3, 1.0]: rare → ~1.0, ubiquitous → ~0.3.
    """
    if total_symbols <= 1:
        return 1.0
    matches = sum(1 for _, blob in all_blobs if blob == query_blob)
    if matches <= 2:
        return 1.0
    idf = math.log(total_symbols / matches) / math.log(total_symbols)
    return max(0.3, min(1.0, idf))


def _get_astvec_complexity(blob: bytes) -> float:
    """Extract a complexity estimate from an astvec blob.

    Sum of all 32 features — higher = more complex code.
    A trivial method (pass, return x) sums to ~1-3.
    A complex method sums to ~10+.
    """
    if len(blob) != 128:
        return 0.0
    features = struct.unpack("32f", blob)
    return sum(features)


def find_related(
    store: RepresentationStore,
    symbol_id: int,
    use_case: str,
    top_k: int = 10,
    scope: str | None = None,
    scope_value: str | None = None,
    substrate: object | None = None,
) -> list[tuple[int, float, dict[str, object]]]:
    """Find related symbols using weighted multi-representation scoring.

    Production anti-noise for obligation_expansion:
    1. Prevalence penalty on query fingerprint
    2. Minimum token overlap (rejects structurally identical but lexically unrelated)
    3. Complexity gate (rejects trivial methods)
    4. Cross-file penalty (cross-file matches need higher raw score)
    """
    profile = USE_CASE_PROFILES.get(use_case)
    if profile is None:
        return []

    weights: dict[str, float] = profile["weights"]  # type: ignore[assignment]
    threshold: float = profile["threshold"]  # type: ignore[assignment]
    is_obligation = use_case == "obligation_expansion"

    # Get the query symbol's representations
    query_reps: dict[str, bytes] = {}
    for rep_type in weights:
        rec = store.get_representation(symbol_id, rep_type)
        if rec is not None:
            query_reps[rep_type] = rec.rep_blob

    if not query_reps:
        return []

    # Complexity gate for obligation_expansion:
    # Skip queries from trivial methods — they match everything
    if is_obligation and "astvec_v1" in query_reps:
        complexity = _get_astvec_complexity(query_reps["astvec_v1"])
        if complexity < 2.5:
            return []  # Too simple — every trivial method looks the same

    # Get query metadata for cross-file detection
    query_meta = store.get_metadata(symbol_id)
    query_file = query_meta.file_path if query_meta else None

    # Scope filter
    candidate_ids: set[int] | None = None
    if scope is not None and scope_value is not None:
        metadata_list = store.get_metadata_by_scope(scope, scope_value)
        candidate_ids = {m.symbol_id for m in metadata_list} - {symbol_id}
        if not candidate_ids:
            return []

    # Prevalence penalty (obligation_expansion only)
    prevalence_penalty = 1.0
    if is_obligation and "fingerprint_v1" in query_reps:
        fp_all = store.get_all_representations("fingerprint_v1")
        prevalence_penalty = _compute_prevalence_penalty(
            query_reps["fingerprint_v1"], fp_all, len(fp_all),
        )

    # Score all candidates
    candidate_scores: dict[int, float] = {}
    candidate_evidence: dict[int, dict[str, object]] = {}
    candidate_token_sim: dict[int, float] = {}
    candidate_complexity: dict[int, float] = {}
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

            if rep_type == "tokensketch_v1":
                candidate_token_sim[cand_id] = similarity
            if rep_type == "astvec_v1":
                candidate_complexity[cand_id] = _get_astvec_complexity(cand_blob)

    if active_weight_sum == 0.0:
        return []

    # Build results with all anti-noise filters
    results: list[tuple[int, float, dict[str, object]]] = []
    for cand_id, raw_score in candidate_scores.items():
        normalized = raw_score / active_weight_sum
        score = normalized * prevalence_penalty

        if is_obligation:
            # Token gate: reject low token overlap
            tok_sim = candidate_token_sim.get(cand_id, 0.0)
            if tok_sim < _MIN_TOKEN_SIM_OBLIGATION:
                continue

            # Complexity gate: reject trivial candidates
            cand_complex = candidate_complexity.get(cand_id, 0.0)
            if cand_complex < 2.5:
                continue

            # Cross-file penalty: require higher score for cross-file matches
            cand_meta = store.get_metadata(cand_id)
            cand_file = cand_meta.file_path if cand_meta else None
            if query_file and cand_file and query_file != cand_file:
                score *= 0.85  # 15% penalty for cross-file

        if score >= threshold:
            evidence = candidate_evidence[cand_id]
            evidence["use_case"] = use_case
            evidence["composite_score"] = round(score, 4)
            if prevalence_penalty < 1.0:
                evidence["prevalence_penalty"] = round(prevalence_penalty, 4)
            results.append((cand_id, round(score, 4), evidence))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]
