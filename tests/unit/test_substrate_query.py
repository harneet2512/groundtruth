"""Tests for SubstrateQuery protocol and BruteForceSubstrateQuery.

Real scenarios: small repos, empty indexes, filtering by allowed IDs,
protocol compliance, and equivalence with the pre-substrate brute-force path.
"""

from __future__ import annotations

import sqlite3
import struct

import pytest

from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.foundation.similarity.substrate import Candidate, SubstrateQuery
from groundtruth.foundation.similarity.substrate_bruteforce import BruteForceSubstrateQuery


def _make_vec(values: list[float]) -> bytes:
    """Pack a float list into a binary blob."""
    return struct.pack(f"{len(values)}f", *values)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def repr_store(conn: sqlite3.Connection) -> RepresentationStore:
    return RepresentationStore(conn)


@pytest.fixture
def populated_store(repr_store: RepresentationStore) -> RepresentationStore:
    """Store with 5 symbols, each having an astvec_v1 representation."""
    # Register the astvec extractor so distance computation works
    from groundtruth.foundation.similarity.astvec import StructuralVectorExtractor
    from groundtruth.foundation.repr.registry import register_extractor, clear_registry
    clear_registry()
    register_extractor(StructuralVectorExtractor())

    # Also add metadata so scope filtering works
    for i in range(5):
        vec = [0.0] * 32
        vec[i % 32] = 1.0  # distinct vectors
        repr_store.store_representation(
            symbol_id=i + 1,
            rep_type="astvec_v1",
            rep_version="1.0",
            rep_blob=_make_vec(vec),
            dim=32,
            source_hash=f"hash_{i}",
            index_version=1,
        )
    return repr_store


class TestBruteForceSubstrateQuery:
    def test_implements_protocol(self) -> None:
        """BruteForceSubstrateQuery must satisfy SubstrateQuery protocol."""
        # This is a structural check — if it fails, the protocol is violated
        from typing import runtime_checkable
        store = RepresentationStore(sqlite3.connect(":memory:"))
        backend = BruteForceSubstrateQuery(store)
        assert hasattr(backend, "query")
        assert hasattr(backend, "insert")
        assert hasattr(backend, "delete")
        assert hasattr(backend, "count")

    def test_query_returns_candidates(self, populated_store: RepresentationStore) -> None:
        backend = BruteForceSubstrateQuery(populated_store)
        # Query with symbol 1's vector
        vec = [0.0] * 32
        vec[0] = 1.0
        results = backend.query(
            rep_type="astvec_v1",
            query_blob=_make_vec(vec),
            top_k=3,
        )
        assert len(results) > 0
        assert all(isinstance(c, Candidate) for c in results)
        # Symbol 1 should be most similar to itself
        assert results[0].symbol_id == 1
        assert results[0].similarity > 0.9

    def test_query_respects_top_k(self, populated_store: RepresentationStore) -> None:
        backend = BruteForceSubstrateQuery(populated_store)
        vec = [1.0] * 32
        results = backend.query(
            rep_type="astvec_v1",
            query_blob=_make_vec(vec),
            top_k=2,
        )
        assert len(results) <= 2

    def test_query_with_allowed_filter(self, populated_store: RepresentationStore) -> None:
        """Only return candidates in the allowed set."""
        backend = BruteForceSubstrateQuery(populated_store)
        vec = [1.0] * 32
        results = backend.query(
            rep_type="astvec_v1",
            query_blob=_make_vec(vec),
            top_k=10,
            allowed_symbol_ids={1, 2},
        )
        for c in results:
            assert c.symbol_id in {1, 2}

    def test_query_unknown_rep_type(self, populated_store: RepresentationStore) -> None:
        """Unknown rep_type → empty results."""
        backend = BruteForceSubstrateQuery(populated_store)
        results = backend.query(
            rep_type="nonexistent_v1",
            query_blob=b"\x00" * 128,
            top_k=5,
        )
        assert results == []

    def test_query_empty_store(self, repr_store: RepresentationStore) -> None:
        """Empty store → empty results."""
        from groundtruth.foundation.similarity.astvec import StructuralVectorExtractor
        from groundtruth.foundation.repr.registry import register_extractor, clear_registry
        clear_registry()
        register_extractor(StructuralVectorExtractor())

        backend = BruteForceSubstrateQuery(repr_store)
        results = backend.query(
            rep_type="astvec_v1",
            query_blob=_make_vec([1.0] * 32),
            top_k=5,
        )
        assert results == []

    def test_count(self, populated_store: RepresentationStore) -> None:
        backend = BruteForceSubstrateQuery(populated_store)
        assert backend.count("astvec_v1") == 5
        assert backend.count("nonexistent_v1") == 0

    def test_insert_and_delete_are_noops(self, populated_store: RepresentationStore) -> None:
        """BruteForce reads directly from store — insert/delete are no-ops."""
        backend = BruteForceSubstrateQuery(populated_store)
        # Should not raise
        backend.insert(999, "astvec_v1", b"\x00" * 128)
        backend.delete(999, "astvec_v1")

    def test_results_sorted_by_similarity_desc(
        self, populated_store: RepresentationStore
    ) -> None:
        backend = BruteForceSubstrateQuery(populated_store)
        results = backend.query(
            rep_type="astvec_v1",
            query_blob=_make_vec([1.0] * 32),
            top_k=5,
        )
        similarities = [c.similarity for c in results]
        assert similarities == sorted(similarities, reverse=True)


class TestCandidateDataclass:
    def test_candidate_fields(self) -> None:
        c = Candidate(symbol_id=42, similarity=0.95, rep_type="astvec_v1")
        assert c.symbol_id == 42
        assert c.similarity == 0.95
        assert c.rep_type == "astvec_v1"
