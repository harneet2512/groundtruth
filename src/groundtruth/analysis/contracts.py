"""Deterministic contract extraction from the symbol index.

Extracts behavioral contracts (returns_value, many_callers, pure, mutates_self)
from stored symbol signatures, documentation, and reference counts.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Err


@dataclass(frozen=True)
class Contract:
    """A behavioral contract for a symbol."""

    symbol_name: str
    file_path: str
    kind: str  # 'returns_value' | 'many_callers' | 'pure' | 'mutates_self'
    description: str
    confidence: float  # 0.0 to 1.0


def extract_contracts(
    store: SymbolStore,
    file_path: str | None = None,
    confidence_threshold: float = 0.8,
) -> list[Contract]:
    """Extract deterministic contracts from the symbol index.

    If file_path is provided, only extract contracts for symbols in that file.
    Returns contracts above the confidence_threshold.
    """
    contracts: list[Contract] = []

    if file_path:
        result = store.get_symbols_in_file(file_path)
    else:
        result = store.get_hotspots(limit=100)

    if isinstance(result, Err):
        return []

    symbols: list[SymbolRecord] = result.value

    for sym in symbols:
        contracts.extend(_contracts_for_symbol(store, sym))

    return [c for c in contracts if c.confidence >= confidence_threshold]


def _contracts_for_symbol(store: SymbolStore, sym: SymbolRecord) -> list[Contract]:
    """Extract contracts for a single symbol."""
    contracts: list[Contract] = []

    # returns_value: return type is not None/void
    if sym.return_type and sym.return_type.lower() not in ("none", "void", ""):
        contracts.append(
            Contract(
                symbol_name=sym.name,
                file_path=sym.file_path,
                kind="returns_value",
                description=f"returns {sym.return_type}",
                confidence=0.90,
            )
        )

    # many_callers: usage_count > 10
    if sym.usage_count > 10:
        contracts.append(
            Contract(
                symbol_name=sym.name,
                file_path=sym.file_path,
                kind="many_callers",
                description=f"{sym.usage_count} callers — changes have wide blast radius",
                confidence=0.95,
            )
        )

    # pure / mutates_self from docstring
    doc = (sym.documentation or "").lower()
    if doc:
        if "returns new" in doc or "returns a new" in doc or "returns a copy" in doc:
            contracts.append(
                Contract(
                    symbol_name=sym.name,
                    file_path=sym.file_path,
                    kind="pure",
                    description="returns new object (do not mutate in place)",
                    confidence=0.85,
                )
            )
        if "mutates" in doc or "in-place" in doc or "in place" in doc or "modifies self" in doc:
            contracts.append(
                Contract(
                    symbol_name=sym.name,
                    file_path=sym.file_path,
                    kind="mutates_self",
                    description="mutates receiver in place",
                    confidence=0.85,
                )
            )

    return contracts
