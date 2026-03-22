"""HnswSubstrateQuery — ANN search via hnswlib (optional dependency).

Requires: pip install groundtruth[hnsw]
Index files stored at: .groundtruth/hnsw_{rep_type}.bin

This backend is candidate generation only — final weighted scoring
stays in composite.py.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from groundtruth.foundation.similarity.substrate import Candidate


def _decode_blob(blob: bytes, dim: int) -> list[float]:
    """Decode a float32 blob into a list of floats."""
    return list(struct.unpack(f"{dim}f", blob[:dim * 4]))


class HnswSubstrateQuery:
    """HNSW-backed ANN search. O(log N) queries.

    Lazily loads indexes from disk. Creates new indexes on insert.
    """

    def __init__(
        self,
        index_dir: str,
        dim: int = 32,
        max_elements: int = 100_000,
        M: int = 16,
        ef_construction: int = 200,
        ef_search: int = 50,
    ) -> None:
        try:
            import hnswlib  # type: ignore[import-untyped]
            self._hnswlib = hnswlib
        except ImportError as exc:
            raise ImportError(
                "hnswlib is required for HNSW backend. "
                "Install with: pip install groundtruth[hnsw]"
            ) from exc

        self._index_dir = index_dir
        self._dim = dim
        self._max_elements = max_elements
        self._M = M
        self._ef_construction = ef_construction
        self._ef_search = ef_search
        self._indexes: dict[str, object] = {}  # rep_type → hnswlib.Index

        os.makedirs(index_dir, exist_ok=True)

    def _get_or_create_index(self, rep_type: str) -> object:
        """Load index from disk or create new one."""
        if rep_type in self._indexes:
            return self._indexes[rep_type]

        index = self._hnswlib.Index(space="cosine", dim=self._dim)
        index_path = os.path.join(self._index_dir, f"hnsw_{rep_type}.bin")

        if os.path.exists(index_path):
            index.load_index(index_path, max_elements=self._max_elements)
        else:
            index.init_index(
                max_elements=self._max_elements,
                M=self._M,
                ef_construction=self._ef_construction,
            )

        index.set_ef(self._ef_search)
        self._indexes[rep_type] = index
        return index

    def query(
        self,
        *,
        rep_type: str,
        query_blob: bytes,
        top_k: int,
        index_version: int | None = None,
        allowed_symbol_ids: set[int] | None = None,
    ) -> list[Candidate]:
        """Query HNSW index for nearest neighbors."""
        index = self._get_or_create_index(rep_type)
        if index.get_current_count() == 0:  # type: ignore[union-attr]
            return []

        query_vec = _decode_blob(query_blob, self._dim)
        # Over-fetch for post-filtering
        fetch_k = min(top_k * 3, index.get_current_count())  # type: ignore[union-attr]

        labels, distances = index.knn_query([query_vec], k=fetch_k)  # type: ignore[union-attr]

        results: list[Candidate] = []
        for label, dist in zip(labels[0], distances[0]):
            sid = int(label)
            if allowed_symbol_ids is not None and sid not in allowed_symbol_ids:
                continue
            # Cosine distance → similarity: 1 - dist (hnswlib cosine = 1 - cos_sim)
            similarity = 1.0 - float(dist)
            results.append(Candidate(
                symbol_id=sid,
                similarity=round(similarity, 4),
                rep_type=rep_type,
            ))
            if len(results) >= top_k:
                break

        return results

    def insert(self, symbol_id: int, rep_type: str, blob: bytes) -> None:
        """Add a vector to the HNSW index."""
        index = self._get_or_create_index(rep_type)
        vec = _decode_blob(blob, self._dim)
        # Resize if needed
        if index.get_current_count() >= index.get_max_elements():  # type: ignore[union-attr]
            index.resize_index(index.get_max_elements() * 2)  # type: ignore[union-attr]
        index.add_items([vec], [symbol_id])  # type: ignore[union-attr]

    def delete(self, symbol_id: int, rep_type: str) -> None:
        """Mark a vector as deleted in the HNSW index."""
        if rep_type in self._indexes:
            try:
                self._indexes[rep_type].mark_deleted(symbol_id)  # type: ignore[union-attr]
            except RuntimeError:
                pass  # ID not in index

    def count(self, rep_type: str) -> int:
        """Number of indexed vectors of this type."""
        if rep_type not in self._indexes:
            index_path = os.path.join(self._index_dir, f"hnsw_{rep_type}.bin")
            if not os.path.exists(index_path):
                return 0
        index = self._get_or_create_index(rep_type)
        return index.get_current_count()  # type: ignore[union-attr]

    def save(self, rep_type: str | None = None) -> None:
        """Persist index(es) to disk."""
        types = [rep_type] if rep_type else list(self._indexes.keys())
        for rt in types:
            if rt in self._indexes:
                path = os.path.join(self._index_dir, f"hnsw_{rt}.bin")
                self._indexes[rt].save_index(path)  # type: ignore[union-attr]
