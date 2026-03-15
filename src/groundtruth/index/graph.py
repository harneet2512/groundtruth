"""Import graph traversal — pure deterministic, no AI."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


@dataclass
class FileNode:
    """A file in the import graph with metadata."""

    path: str
    distance: int
    symbols: list[str]


@dataclass
class Reference:
    """A reference to a symbol from a specific location."""

    file_path: str
    line: int | None
    context: str


@dataclass
class ImpactResult:
    """Result of an impact analysis."""

    symbol_name: str
    impacted_files: list[str]
    impact_radius: int


class ImportGraph:
    """BFS/DFS traversal over the import graph stored in SQLite."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def find_connected_files(
        self, entry_files: list[str], max_depth: int = 3
    ) -> Result[list[FileNode], GroundTruthError]:
        """BFS from entry files over import relationships (bidirectional)."""
        visited: dict[str, int] = {}  # file_path -> distance
        file_symbols: dict[str, list[str]] = {}  # file_path -> symbol names
        queue: deque[tuple[str, int]] = deque()

        for f in entry_files:
            if f not in visited:
                visited[f] = 0
                file_symbols[f] = []
                queue.append((f, 0))

        while queue:
            current_file, depth = queue.popleft()

            if depth >= max_depth:
                continue

            # Forward: what does this file import?
            imports_result = self._store.get_imports_for_file(current_file)
            if isinstance(imports_result, Err):
                return Err(imports_result.error)

            for ref in imports_result.value:
                sym_result = self._store.get_symbol_by_id(ref.symbol_id)
                if isinstance(sym_result, Err):
                    return Err(sym_result.error)
                sym = sym_result.value
                if sym is not None:
                    target_file = sym.file_path
                    if target_file not in file_symbols:
                        file_symbols[target_file] = []
                    if sym.name not in file_symbols[target_file]:
                        file_symbols[target_file].append(sym.name)
                    if target_file not in visited:
                        visited[target_file] = depth + 1
                        queue.append((target_file, depth + 1))

            # Backward: who imports from this file?
            importers_result = self._store.get_importers_of_file(current_file)
            if isinstance(importers_result, Err):
                return Err(importers_result.error)

            for importer_file in importers_result.value:
                if importer_file not in visited:
                    visited[importer_file] = depth + 1
                    if importer_file not in file_symbols:
                        file_symbols[importer_file] = []
                    queue.append((importer_file, depth + 1))

        nodes = [
            FileNode(path=path, distance=dist, symbols=file_symbols.get(path, []))
            for path, dist in visited.items()
        ]
        nodes.sort(key=lambda n: (n.distance, n.path))
        return Ok(nodes)

    def find_callers(self, symbol_name: str) -> Result[list[Reference], GroundTruthError]:
        """All files/lines that reference this symbol."""
        symbols_result = self._store.find_symbol_by_name(symbol_name)
        if isinstance(symbols_result, Err):
            return Err(symbols_result.error)

        seen: set[tuple[str, int | None]] = set()
        refs: list[Reference] = []

        for sym in symbols_result.value:
            refs_result = self._store.get_refs_for_symbol(sym.id)
            if isinstance(refs_result, Err):
                return Err(refs_result.error)
            for ref in refs_result.value:
                key = (ref.referenced_in_file, ref.referenced_at_line)
                if key not in seen:
                    seen.add(key)
                    refs.append(
                        Reference(
                            file_path=ref.referenced_in_file,
                            line=ref.referenced_at_line,
                            context="",
                        )
                    )

        return Ok(refs)

    def find_callees(
        self, symbol_name: str, file_path: str
    ) -> Result[list[Reference], GroundTruthError]:
        """All symbols referenced by code in a given file."""
        _ = symbol_name  # used for scoping in future, currently we get all refs from file
        refs_result = self._store.get_refs_from_file(file_path)
        if isinstance(refs_result, Err):
            return Err(refs_result.error)

        seen: set[int] = set()
        callees: list[Reference] = []

        for ref in refs_result.value:
            if ref.symbol_id in seen:
                continue
            seen.add(ref.symbol_id)
            sym_result = self._store.get_symbol_by_id(ref.symbol_id)
            if isinstance(sym_result, Err):
                return Err(sym_result.error)
            sym = sym_result.value
            if sym is not None:
                callees.append(
                    Reference(
                        file_path=sym.file_path,
                        line=sym.line_number,
                        context="",
                    )
                )

        return Ok(callees)

    def get_impact_radius(self, symbol_name: str) -> Result[ImpactResult, GroundTruthError]:
        """How many files break if this symbol changes?"""
        symbols_result = self._store.find_symbol_by_name(symbol_name)
        if isinstance(symbols_result, Err):
            return Err(symbols_result.error)

        impacted: set[str] = set()

        for sym in symbols_result.value:
            refs_result = self._store.get_refs_for_symbol(sym.id)
            if isinstance(refs_result, Err):
                return Err(refs_result.error)
            for ref in refs_result.value:
                impacted.add(ref.referenced_in_file)

        impacted_list = sorted(impacted)
        return Ok(
            ImpactResult(
                symbol_name=symbol_name,
                impacted_files=impacted_list,
                impact_radius=len(impacted_list),
            )
        )
