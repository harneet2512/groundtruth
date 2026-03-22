"""Parser registry — auto-selects the best available backend.

Priority: tree-sitter (multi-language) > Python ast (Python-only fallback).
"""

from __future__ import annotations

from groundtruth.foundation.parser.protocol import SymbolExtractor
from groundtruth.utils.logger import get_logger

logger = get_logger(__name__)

_extractor: SymbolExtractor | None = None
_initialized = False


def _init() -> None:
    """Initialize the parser registry, selecting the best available backend."""
    global _extractor, _initialized
    if _initialized:
        return
    _initialized = True

    # Try tree-sitter first
    try:
        from groundtruth.foundation.parser.treesitter_backend import (
            HAS_TREE_SITTER,
            TreeSitterExtractor,
        )

        if HAS_TREE_SITTER:
            ts = TreeSitterExtractor()
            langs = ts.supported_languages
            if langs:
                _extractor = ts
                logger.info(
                    "parser_backend_selected",
                    backend="tree-sitter",
                    languages=langs,
                )
                return
    except Exception:
        pass

    # Fall back to Python AST
    from groundtruth.foundation.parser.ast_backend import PythonASTExtractor

    _extractor = PythonASTExtractor()
    logger.info(
        "parser_backend_selected",
        backend="python-ast",
        languages=["python"],
    )


def get_extractor() -> SymbolExtractor:
    """Get the best available symbol extractor."""
    _init()
    assert _extractor is not None
    return _extractor


def get_supported_languages() -> list[str]:
    """Get list of languages the current extractor supports."""
    return get_extractor().supported_languages


def get_extractor_for_language(language: str) -> SymbolExtractor | None:
    """Get an extractor that supports the given language, or None."""
    ext = get_extractor()
    if language in ext.supported_languages:
        return ext
    return None
