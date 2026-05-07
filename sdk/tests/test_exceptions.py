from __future__ import annotations

from groundtruth.exceptions import (
    GraphNotFoundError,
    GroundTruthError,
    InvalidDiffError,
    SchemaVersionError,
    SymbolNotFoundError,
)


def test_exception_hierarchy() -> None:
    assert issubclass(GraphNotFoundError, GroundTruthError)
    assert issubclass(SymbolNotFoundError, GroundTruthError)
    assert issubclass(SchemaVersionError, GroundTruthError)
    assert issubclass(InvalidDiffError, GroundTruthError)
