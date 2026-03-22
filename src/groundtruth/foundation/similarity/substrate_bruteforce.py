"""BruteForceSubstrateQuery — O(N) scan over all representations.

This is the default backend. It wraps the existing get_all_representations()
loop from composite.py into the SubstrateQuery interface, preserving
identical behavior to the pre-substrate code.
"""

from __future__ import annotations

from groundtruth.foundation.repr.registry import get_extractor
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.foundation.similarity.substrate import Candidate


class BruteForceSubstrateQuery:
    """Brute-force KNN: loads all representations, computes distances."""

    def __init__(self, store: RepresentationStore) -> None:
        self._store = store

    def query(
        self,
        *,
        rep_type: str,
        query_blob: bytes,
        top_k: int,
        index_version: int | None = None,
        allowed_symbol_ids: set[int] | None = None,
    ) -> list[Candidate]:
        """Scan all representations, compute distance, return top-k."""
        extractor = get_extractor(rep_type)
        if extractor is None:
            return []

        all_reps = self._store.get_all_representations(rep_type)
        results: list[Candidate] = []

        for cand_id, cand_blob in all_reps:
            if allowed_symbol_ids is not None and cand_id not in allowed_symbol_ids:
                continue
            dist = extractor.distance(query_blob, cand_blob)
            similarity = 1.0 - dist
            results.append(Candidate(
                symbol_id=cand_id,
                similarity=round(similarity, 4),
                rep_type=rep_type,
            ))

        results.sort(key=lambda c: c.similarity, reverse=True)
        return results[:top_k]

    def insert(self, symbol_id: int, rep_type: str, blob: bytes) -> None:
        """No-op: BruteForce reads directly from RepresentationStore."""
        pass

    def delete(self, symbol_id: int, rep_type: str) -> None:
        """No-op: BruteForce reads directly from RepresentationStore."""
        pass

    def count(self, rep_type: str) -> int:
        """Count stored representations of this type."""
        return len(self._store.get_all_representations(rep_type))
