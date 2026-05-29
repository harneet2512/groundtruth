"""Import graph traversal — pure deterministic, no AI."""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

# C7 (RF-4): closure-read gates. Mirror the Go-side closure build
# (closure.MaxDepth / closure.MinEdgeConfidence) so a depth-3, conf-0.5
# closure read returns exactly the verified transitive reach the indexer
# persisted. Readers NEVER relax these below the build-time floor.
_CLOSURE_MAX_DEPTH = 3
_CLOSURE_MIN_CONFIDENCE = 0.5


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
        self,
        entry_files: list[str],
        max_depth: int = 3,
        max_visited: int = 500,
    ) -> Result[list[FileNode], GroundTruthError]:
        """BFS from entry files over import relationships (bidirectional).

        Args:
            max_visited: Stop BFS after visiting this many files to prevent
                         OOM on large repos.
        """
        visited: dict[str, int] = {}  # file_path -> distance
        file_symbols: dict[str, list[str]] = {}  # file_path -> symbol names
        queue: deque[tuple[str, int]] = deque()

        for f in entry_files:
            if f not in visited:
                visited[f] = 0
                file_symbols[f] = []
                queue.append((f, 0))

        while queue:
            if len(visited) >= max_visited:
                break

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

    def _closure_connection(self) -> sqlite3.Connection | None:
        """Return the underlying sqlite3 connection if the store exposes one.

        The C7 closure table is read with a direct indexed SELECT. Only the
        GraphStore bridge exposes a raw connection; an in-memory / Python-indexer
        SymbolStore may too, but if anything about the access raises we treat it
        as "no closure available" and let the caller fall back to live BFS.
        """
        try:
            conn = getattr(self._store, "connection", None)
        except Exception:
            return None
        if isinstance(conn, sqlite3.Connection):
            return conn
        return None

    def _closure_sources_for_symbol(self, symbol_id: int) -> set[int] | None:
        """Node IDs that transitively reach ``symbol_id`` via the closure table.

        Closure semantics: a row (source_id, target_id, depth, min_confidence)
        means source_id reaches target_id. "Who is impacted if symbol_id
        changes / who transitively calls it" = all source_id with
        target_id = symbol_id, gated to depth<=3 and min_confidence>=0.5
        (RF-4 verified reach).

        Returns the set of source node IDs, or ``None`` if the closure table is
        absent (old graph.db) — the signal to fall back to live BFS.
        """
        conn = self._closure_connection()
        if conn is None:
            return None
        try:
            cursor = conn.execute(
                "SELECT DISTINCT source_id FROM closure "
                "WHERE target_id = ? AND depth <= ? AND min_confidence >= ?",
                (symbol_id, _CLOSURE_MAX_DEPTH, _CLOSURE_MIN_CONFIDENCE),
            )
            return {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            # No closure table (pre-C7 graph.db) → caller falls back to BFS.
            return None

    def _files_for_node_ids(self, node_ids: set[int]) -> dict[int, str]:
        """Map node IDs to file paths via get_symbol_by_id (interface-only)."""
        out: dict[int, str] = {}
        for nid in node_ids:
            sym_result = self._store.get_symbol_by_id(nid)
            if isinstance(sym_result, Ok) and sym_result.value is not None:
                out[nid] = sym_result.value.file_path
        return out

    def find_callers(self, symbol_name: str) -> Result[list[Reference], GroundTruthError]:
        """All files that transitively call this symbol (verified reach).

        Prefers the C7 closure table (depth<=3, min_confidence>=0.5) when
        present, giving transitive callers via one indexed SELECT. Falls back
        to the live 1-hop BFS over edges when the closure table is absent
        (old graph.db) — zero regression on pre-C7 databases.
        """
        symbols_result = self._store.find_symbol_by_name(symbol_name)
        if isinstance(symbols_result, Err):
            return Err(symbols_result.error)

        # --- C7 closure fast path ---
        closure_files: set[str] = set()
        closure_used = False
        for sym in symbols_result.value:
            sources = self._closure_sources_for_symbol(sym.id)
            if sources is None:
                closure_used = False
                break
            closure_used = True
            file_map = self._files_for_node_ids(sources)
            closure_files.update(f for f in file_map.values() if f)

        if closure_used:
            refs = [
                Reference(file_path=fp, line=None, context="")
                for fp in sorted(closure_files)
            ]
            return Ok(refs)

        # --- Live BFS fallback (pre-C7 graph.db) ---
        seen: set[tuple[str, int | None]] = set()
        refs = []

        for sym in symbols_result.value:
            try:
                refs_result = self._store.get_refs_for_symbol(sym.id, min_confidence=0.5)  # type: ignore[call-arg]
            except TypeError:
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
        """Symbols called from a given file (file-scoped; RefRecord lacks source function ID)."""
        _ = symbol_name
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
        """How many files break if this symbol changes? (transitive when possible)

        Prefers the C7 closure table (transitive reach, depth<=3,
        min_confidence>=0.5) so the blast radius reflects indirect callers, not
        just direct ones. Falls back to the live 1-hop edge query when the
        closure table is absent (old graph.db) — zero regression.
        """
        symbols_result = self._store.find_symbol_by_name(symbol_name)
        if isinstance(symbols_result, Err):
            return Err(symbols_result.error)

        impacted: set[str] = set()

        # --- C7 closure fast path ---
        closure_used = False
        for sym in symbols_result.value:
            sources = self._closure_sources_for_symbol(sym.id)
            if sources is None:
                closure_used = False
                impacted.clear()
                break
            closure_used = True
            file_map = self._files_for_node_ids(sources)
            impacted.update(f for f in file_map.values() if f)

        if not closure_used:
            # --- Live BFS fallback (pre-C7 graph.db) ---
            for sym in symbols_result.value:
                try:
                    refs_result = self._store.get_refs_for_symbol(sym.id, min_confidence=0.5)  # type: ignore[call-arg]
                except TypeError:
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
