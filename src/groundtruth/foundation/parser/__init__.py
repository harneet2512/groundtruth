"""Parser abstraction — language-agnostic symbol extraction."""

from groundtruth.foundation.parser.protocol import (
    ExtractedSymbol,
    ParsedFile,
    SymbolExtractor,
)
from groundtruth.foundation.parser.registry import get_extractor, get_supported_languages

__all__ = [
    "ExtractedSymbol",
    "ParsedFile",
    "SymbolExtractor",
    "get_extractor",
    "get_supported_languages",
]
