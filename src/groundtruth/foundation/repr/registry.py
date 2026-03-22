"""Representation registry — components register extraction strategies.

Each representation type (fingerprint, structural vector, token sketch, etc.)
registers itself here. The indexing pipeline queries the registry for all
registered extractors and runs each one per symbol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from groundtruth.foundation.parser.protocol import ExtractedSymbol


@runtime_checkable
class RepresentationExtractor(Protocol):
    """Protocol for representation extraction from symbols."""

    @property
    def rep_type(self) -> str:
        """Unique identifier for this representation (e.g., 'fingerprint_v1')."""
        ...

    @property
    def rep_version(self) -> str:
        """Version string for this extractor."""
        ...

    @property
    def dimension(self) -> int | None:
        """Dimensionality for vector types. None for fingerprints/sketches."""
        ...

    @property
    def supported_languages(self) -> list[str]:
        """Languages this extractor supports."""
        ...

    def extract(self, symbol: ExtractedSymbol) -> bytes:
        """Extract a representation from a symbol. Returns raw bytes."""
        ...

    def distance(self, a: bytes, b: bytes) -> float:
        """Compute distance between two representations. 0.0 = identical, 1.0 = maximally different."""
        ...

    def invalidation_key(self, file_path: str, content: str) -> str:
        """Content hash for staleness detection."""
        ...


# Global registry
_registry: dict[str, RepresentationExtractor] = {}


def register_extractor(extractor: RepresentationExtractor) -> None:
    """Register a representation extractor."""
    _registry[extractor.rep_type] = extractor


def get_registry() -> dict[str, RepresentationExtractor]:
    """Get all registered extractors."""
    return dict(_registry)


def get_extractor(rep_type: str) -> RepresentationExtractor | None:
    """Get a specific extractor by type name."""
    return _registry.get(rep_type)


def clear_registry() -> None:
    """Clear all registered extractors (for testing)."""
    _registry.clear()
