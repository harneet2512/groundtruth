"""SubstrateQuery protocol — abstract interface for KNN search.

Implementations: BruteForceSubstrateQuery (default), HnswSubstrateQuery (optional).
find_related() dispatches to whichever backend is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Candidate:
    """A candidate from a substrate query."""

    symbol_id: int
    similarity: float
    rep_type: str


class SubstrateQuery(Protocol):
    """Abstract interface for nearest-neighbor search over representations.

    Implementations must support query, insert, delete, and count.
    """

    def query(
        self,
        *,
        rep_type: str,
        query_blob: bytes,
        top_k: int,
        index_version: int | None = None,
        allowed_symbol_ids: set[int] | None = None,
    ) -> list[Candidate]:
        """Return nearest candidates sorted by similarity descending."""
        ...

    def insert(self, symbol_id: int, rep_type: str, blob: bytes) -> None:
        """Insert or update a representation."""
        ...

    def delete(self, symbol_id: int, rep_type: str) -> None:
        """Remove a representation."""
        ...

    def count(self, rep_type: str) -> int:
        """Number of indexed representations of this type."""
        ...
