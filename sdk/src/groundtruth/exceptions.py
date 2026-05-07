"""Error hierarchy for the GroundTruth SDK."""

from __future__ import annotations


class GroundTruthError(Exception):
    """Base exception for all GT SDK errors."""


class GraphNotFoundError(GroundTruthError):
    """``graph.db`` path does not exist or is not a file."""


class SchemaVersionError(GroundTruthError):
    """``graph.db`` schema does not match expected version."""


class SymbolNotFoundError(GroundTruthError):
    """Symbol query resolved to zero matches in the graph."""


class InvalidDiffError(GroundTruthError):
    """Unified diff string could not be parsed (reserved for validators)."""
