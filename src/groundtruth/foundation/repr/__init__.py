"""Representation registry and storage — multi-representation substrate."""

from groundtruth.foundation.repr.registry import (
    RepresentationExtractor,
    get_registry,
    register_extractor,
)
from groundtruth.foundation.repr.store import RepresentationStore

__all__ = [
    "RepresentationExtractor",
    "RepresentationStore",
    "get_registry",
    "register_extractor",
]
