"""Shared symbol resolution helpers."""

from __future__ import annotations

from typing import Any


def _symbol_file_path(symbol: Any) -> str | None:
    if isinstance(symbol, dict):
        return symbol.get("file_path")
    return getattr(symbol, "file_path", None)


def normalize_file_path(file_path: str | None) -> str | None:
    if not file_path:
        return None
    return file_path.replace("\\", "/").lstrip("./")


def select_symbol(symbols: list[Any], file_path: str | None = None) -> Any | None:
    """Choose a single symbol with optional file scoping."""
    if not symbols:
        return None

    normalized = normalize_file_path(file_path)
    if not normalized:
        return symbols[0] if len(symbols) == 1 else None

    exact = [
        symbol
        for symbol in symbols
        if normalize_file_path(_symbol_file_path(symbol)) == normalized
    ]
    if len(exact) == 1:
        return exact[0]

    scoped = [
        symbol
        for symbol in symbols
        if (
            normalize_file_path(_symbol_file_path(symbol)) == normalized
            or (normalize_file_path(_symbol_file_path(symbol)) or "").endswith("/" + normalized)
        )
    ]
    if len(scoped) == 1:
        return scoped[0]

    return None


def resolve_unique_symbol_file(symbols: list[Any], file_path: str | None = None) -> dict[str, Any]:
    """Resolve a symbol match set to a single file or ambiguity state."""
    if not symbols:
        return {"status": "missing", "file_path": None, "matches": []}

    selected = select_symbol(symbols, file_path)
    if selected is not None:
        selected_path = _symbol_file_path(selected)
        return {"status": "resolved", "file_path": selected_path, "matches": [selected_path]}

    normalized = normalize_file_path(file_path)
    if normalized:
        scoped_matches = [
            _symbol_file_path(symbol)
            for symbol in symbols
            if (
                normalize_file_path(_symbol_file_path(symbol)) == normalized
                or (normalize_file_path(_symbol_file_path(symbol)) or "").endswith("/" + normalized)
            )
        ]
        if scoped_matches:
            return {"status": "ambiguous", "file_path": None, "matches": scoped_matches}

    return {
        "status": "ambiguous",
        "file_path": None,
        "matches": [_symbol_file_path(symbol) for symbol in symbols],
    }
