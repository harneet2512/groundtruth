"""Bridge layer: reads Go indexer's nodes/edges schema, exposes SymbolStore interface.

The Go indexer (gt-index) writes to a different SQLite schema:
  - nodes(id, label, name, qualified_name, file_path, start_line, end_line,
          signature, return_type, is_exported, is_test, language, parent_id)
  - edges(id, source_id, target_id, type, source_line, source_file,
          resolution_method, metadata)
  - properties(id, node_id, kind, value, line, confidence)  [v16+]
  - assertions(id, test_node_id, target_node_id, kind, expression, expected, line)  [v16+]

This module maps that schema to SymbolRecord/RefRecord so the viz, RiskScorer,
CLI, and MCP tools work unchanged.
"""

from __future__ import annotations

import sqlite3
import time

import os
from typing import Any

from groundtruth.index.store import (
    BriefingLogRecord,
    PackageRecord,
    RefRecord,
    SymbolRecord,
    SymbolStore,
)
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


# Go indexer label → SymbolStore kind
_LABEL_TO_KIND: dict[str, str] = {
    "Function": "function",
    "Class": "class",
    "Method": "method",
    "Interface": "interface",
    "Struct": "class",
    "Enum": "enum",
    "Type": "type",
    "File": "variable",
    "Variable": "variable",
    "Constant": "variable",
    "Property": "property",
    "Field": "property",
}

# Go indexer edge type → refs reference_type
_EDGE_TYPE_TO_REF: dict[str, str] = {
    "CALLS": "call",
    "IMPORTS": "import",
    "DEFINES": "call",
    "INHERITS": "type_usage",
    "IMPLEMENTS": "type_usage",
}


def is_graph_db(db_path: str) -> bool:
    """Detect whether a SQLite DB uses the Go indexer schema (nodes/edges).

    Validates both table existence AND required columns to avoid false
    positives from corrupted databases.
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('nodes', 'edges')"
        )
        tables = {row[0] for row in cursor.fetchall()}
        if "nodes" not in tables or "edges" not in tables:
            conn.close()
            return False

        # Validate required columns exist
        node_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        edge_cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        conn.close()

        required_node_cols = {"id", "name", "label", "file_path", "language"}
        required_edge_cols = {"id", "source_id", "target_id", "type"}

        return required_node_cols.issubset(node_cols) and required_edge_cols.issubset(edge_cols)
    except (sqlite3.Error, OSError):
        return False


def _node_row_to_symbol(row: sqlite3.Row, usage_count: int = 0) -> SymbolRecord:
    """Convert a Go indexer node row to a SymbolRecord."""
    label = row["label"] or "Variable"
    return SymbolRecord(
        id=row["id"],
        name=row["name"],
        kind=_LABEL_TO_KIND.get(label, "variable"),
        language=row["language"] or "unknown",
        file_path=row["file_path"],
        line_number=row["start_line"],
        end_line=row["end_line"],
        is_exported=bool(row["is_exported"]),
        signature=row["signature"],
        params=None,  # Go indexer doesn't store params separately
        return_type=row["return_type"],
        documentation=None,  # Go indexer doesn't store docs
        usage_count=usage_count,
        last_indexed_at=int(time.time()),
    )


def _edge_row_to_ref(row: sqlite3.Row) -> RefRecord:
    """Convert a Go indexer edge row to a RefRecord."""
    edge_type = row["type"] or "CALLS"
    return RefRecord(
        id=row["id"],
        symbol_id=row["target_id"],
        referenced_in_file=row["source_file"] or "",
        referenced_at_line=row["source_line"],
        reference_type=_EDGE_TYPE_TO_REF.get(edge_type, "call"),
    )


class GraphStore(SymbolStore):
    """Reads Go indexer's nodes/edges DB through the SymbolStore interface.

    Overrides only the read methods that the viz, RiskScorer, and CLI use.
    Write methods (insert_symbol, etc.) are no-ops since the Go indexer owns writes.
    """

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path=db_path)
        self._usage_cache: dict[int, int] | None = None

    def initialize(self) -> Result[None, GroundTruthError]:
        """Open connection to the Go indexer DB."""
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Pre-compute usage counts (incoming edge count per node)
            self._build_usage_cache()
            return Ok(None)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_init_failed",
                    message=f"Failed to open graph database: {exc}",
                )
            )

    def _build_usage_cache(self) -> None:
        """Compute usage_count for each node as COUNT(incoming edges)."""
        self._usage_cache = {}
        try:
            cursor = self.connection.execute(
                "SELECT target_id, COUNT(*) as cnt FROM edges GROUP BY target_id"
            )
            for row in cursor.fetchall():
                self._usage_cache[row["target_id"]] = row["cnt"]
        except sqlite3.Error:
            self._usage_cache = {}

    def _usage_for(self, node_id: int) -> int:
        if self._usage_cache is None:
            return 0
        return self._usage_cache.get(node_id, 0)

    # --- Symbol Operations (read-only) ---

    def find_symbol_by_name(self, name: str) -> Result[list[SymbolRecord], GroundTruthError]:
        try:
            cursor = self.connection.execute(
                "SELECT * FROM nodes WHERE name = ? OR qualified_name = ?",
                (name, name),
            )
            return Ok(
                [_node_row_to_symbol(row, self._usage_for(row["id"])) for row in cursor.fetchall()]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(code="db_query_failed", message=f"Failed to find symbol: {exc}")
            )

    def get_symbols_in_file(self, file_path: str) -> Result[list[SymbolRecord], GroundTruthError]:
        try:
            cursor = self.connection.execute(
                "SELECT * FROM nodes WHERE file_path = ?", (file_path,)
            )
            return Ok(
                [_node_row_to_symbol(row, self._usage_for(row["id"])) for row in cursor.fetchall()]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed", message=f"Failed to get symbols in file: {exc}"
                )
            )

    def get_symbol_by_id(self, symbol_id: int) -> Result[SymbolRecord | None, GroundTruthError]:
        try:
            cursor = self.connection.execute("SELECT * FROM nodes WHERE id = ?", (symbol_id,))
            row = cursor.fetchone()
            if row is None:
                return Ok(None)
            return Ok(_node_row_to_symbol(row, self._usage_for(row["id"])))
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed", message=f"Failed to get symbol by id: {exc}"
                )
            )

    def get_refs_from_file(
        self, file_path: str, reference_type: str | None = None
    ) -> Result[list[RefRecord], GroundTruthError]:
        """Get all edges originating from a file, mapped to RefRecords."""
        try:
            if reference_type is not None:
                # Reverse-map our ref type back to Go edge types
                go_types = [k for k, v in _EDGE_TYPE_TO_REF.items() if v == reference_type]
                if not go_types:
                    return Ok([])
                placeholders = ",".join("?" for _ in go_types)
                cursor = self.connection.execute(
                    f"SELECT * FROM edges WHERE source_file = ? AND type IN ({placeholders})",
                    (file_path, *go_types),
                )
            else:
                cursor = self.connection.execute(
                    "SELECT * FROM edges WHERE source_file = ?",
                    (file_path,),
                )
            return Ok([_edge_row_to_ref(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed", message=f"Failed to get refs from file: {exc}"
                )
            )

    def get_all_symbol_names(self) -> Result[list[str], GroundTruthError]:
        try:
            cursor = self.connection.execute("SELECT DISTINCT name FROM nodes")
            return Ok([row["name"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed", message=f"Failed to get symbol names: {exc}"
                )
            )

    def get_all_files(self) -> Result[list[str], GroundTruthError]:
        try:
            cursor = self.connection.execute("SELECT DISTINCT file_path FROM nodes")
            return Ok([row["file_path"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(code="db_query_failed", message=f"Failed to get file paths: {exc}")
            )

    def get_importers_of_file(self, file_path: str) -> Result[list[str], GroundTruthError]:
        """Get files that have edges pointing to nodes in this file."""
        try:
            cursor = self.connection.execute(
                """SELECT DISTINCT e.source_file
                   FROM edges e
                   JOIN nodes n ON e.target_id = n.id
                   WHERE n.file_path = ?
                   AND e.source_file IS NOT NULL""",
                (file_path,),
            )
            return Ok([row["source_file"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(code="db_query_failed", message=f"Failed to get importers: {exc}")
            )

    def get_stats(self) -> Result[dict[str, object], GroundTruthError]:
        try:
            stats: dict[str, object] = {}
            cursor = self.connection.execute("SELECT COUNT(*) as cnt FROM nodes")
            row = cursor.fetchone()
            stats["symbols_count"] = row["cnt"] if row else 0

            cursor = self.connection.execute("SELECT COUNT(DISTINCT file_path) as cnt FROM nodes")
            row = cursor.fetchone()
            stats["files_count"] = row["cnt"] if row else 0

            cursor = self.connection.execute("SELECT COUNT(*) as cnt FROM edges")
            row = cursor.fetchone()
            stats["refs_count"] = row["cnt"] if row else 0

            # No interventions table in Go DB
            stats["total_interventions"] = 0
            stats["hallucinations_caught"] = 0
            stats["ai_calls"] = 0
            stats["tokens_used"] = 0

            return Ok(stats)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(code="db_query_failed", message=f"Failed to get stats: {exc}")
            )

    def get_dead_code(self) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get exported nodes with zero incoming edges."""
        try:
            cursor = self.connection.execute(
                """SELECT n.* FROM nodes n
                   WHERE n.is_exported = 1
                   AND NOT EXISTS (
                       SELECT 1 FROM edges e WHERE e.target_id = n.id
                   )
                   ORDER BY n.file_path, n.name"""
            )
            return Ok([_node_row_to_symbol(row, 0) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(code="db_query_failed", message=f"Failed to get dead code: {exc}")
            )

    def get_unused_packages(self) -> Result[list[PackageRecord], GroundTruthError]:
        """Go indexer doesn't track packages — return empty list."""
        return Ok([])

    def get_file_dependencies(
        self,
        max_deps: int = 5000,
    ) -> Result[list[tuple[str, str, str]], GroundTruthError]:
        """Get cross-file dependencies from the edges table.

        Limited to max_deps unique file pairs for performance on large repos.
        """
        try:
            cursor = self.connection.execute(
                """SELECT DISTINCT e.source_file,
                                  n.file_path AS target_file,
                                  e.type
                   FROM edges e
                   JOIN nodes n ON e.target_id = n.id
                   WHERE e.source_file IS NOT NULL
                   AND e.source_file != n.file_path
                   LIMIT ?""",
                (max_deps,),
            )
            return Ok(
                [
                    (
                        row["source_file"],
                        row["target_file"],
                        _EDGE_TYPE_TO_REF.get(row["type"], "call"),
                    )
                    for row in cursor.fetchall()
                ]
            )
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get file dependencies: {exc}",
                )
            )

    def get_all_packages(self) -> Result[list[PackageRecord], GroundTruthError]:
        """Go indexer doesn't track packages — return empty list."""
        return Ok([])

    # --- Methods required by MCP tools.py ---

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize file path for comparison: forward slashes, no leading ./."""
        p = path.replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        return p

    def _match_file_path(self, file_path: str) -> str:
        """Find the actual stored path that matches the given file_path.

        Handles absolute vs relative, forward vs back slashes.
        """
        normalized = self._normalize_path(file_path)
        try:
            # Try exact match first
            cursor = self.connection.execute(
                "SELECT file_path FROM nodes WHERE file_path = ? LIMIT 1",
                (normalized,),
            )
            row = cursor.fetchone()
            if row:
                return row["file_path"]

            # Try suffix match (handles absolute vs relative)
            cursor = self.connection.execute(
                "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE ? LIMIT 1",
                (f"%{normalized}",),
            )
            row = cursor.fetchone()
            if row:
                return row["file_path"]
        except sqlite3.Error:
            pass
        return file_path  # return original if nothing found

    def get_hotspots(self, limit: int = 20) -> Result[list[SymbolRecord], GroundTruthError]:
        """Get the most-referenced symbols (highest incoming edge count)."""
        try:
            cursor = self.connection.execute(
                """SELECT n.*, COUNT(e.id) as usage
                   FROM nodes n
                   JOIN edges e ON e.target_id = n.id
                   WHERE n.is_test = 0
                   GROUP BY n.id
                   ORDER BY usage DESC
                   LIMIT ?""",
                (limit,),
            )
            results = []
            for row in cursor.fetchall():
                sym = _node_row_to_symbol(row, row["usage"])
                results.append(sym)
            return Ok(results)
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(code="db_query_failed", message=f"Failed to get hotspots: {exc}")
            )

    def get_imports_for_file(self, file_path: str) -> Result[list[RefRecord], GroundTruthError]:
        """Get edges originating from a file (what does this file call/import?)."""
        matched = self._match_file_path(file_path)
        try:
            cursor = self.connection.execute(
                "SELECT * FROM edges WHERE source_file = ?",
                (matched,),
            )
            return Ok([_edge_row_to_ref(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get imports for file: {exc}",
                )
            )

    def get_sibling_files(self, file_path: str) -> Result[list[str], GroundTruthError]:
        """Get files in the same directory, excluding the input file."""
        matched = self._match_file_path(file_path)
        directory = os.path.dirname(self._normalize_path(matched))
        if not directory:
            return Ok([])
        try:
            all_result = self.get_all_files()
            if isinstance(all_result, Err):
                return all_result
            siblings = [
                f
                for f in all_result.value
                if self._normalize_path(os.path.dirname(f)) == directory
                and self._normalize_path(f) != self._normalize_path(matched)
            ]
            return Ok(siblings)
        except Exception as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get sibling files: {exc}",
                )
            )

    def get_entry_point_files(self, limit: int = 5) -> Result[list[str], GroundTruthError]:
        """Get files with most incoming references (likely entry points)."""
        try:
            cursor = self.connection.execute(
                """SELECT n.file_path, COUNT(e.id) as cnt
                   FROM nodes n
                   JOIN edges e ON e.target_id = n.id
                   GROUP BY n.file_path
                   ORDER BY cnt DESC
                   LIMIT ?""",
                (limit,),
            )
            return Ok([row["file_path"] for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get entry point files: {exc}",
                )
            )

    def get_top_directories(
        self, limit: int = 10
    ) -> Result[list[dict[str, Any]], GroundTruthError]:
        """Get directories with the most symbols."""
        try:
            cursor = self.connection.execute("SELECT DISTINCT file_path FROM nodes")
            dir_counts: dict[str, int] = {}
            for row in cursor.fetchall():
                d = os.path.dirname(self._normalize_path(row["file_path"]))
                if d:
                    dir_counts[d] = dir_counts.get(d, 0) + 1
            sorted_dirs = sorted(dir_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
            return Ok([{"directory": d, "symbol_count": c, "ref_count": 0} for d, c in sorted_dirs])
        except sqlite3.Error as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get top directories: {exc}",
                )
            )

    def get_briefing_logs_for_file(
        self, file_path: str
    ) -> Result[list[BriefingLogRecord], GroundTruthError]:
        """Go indexer DB has no briefing_logs table — return empty list."""
        return Ok([])

    def insert_briefing_log(  # type: ignore[override]
        self,
        timestamp: int = 0,
        intent: str = "",
        briefing_text: str = "",
        briefing_symbols: list[str] | None = None,
        target_file: str | None = None,
    ) -> Result[int, GroundTruthError]:
        """No-op for Go indexer DB — briefing logs not stored."""
        return Ok(0)

    def link_briefing_to_validation(
        self, log_id: int, validation_id: int
    ) -> Result[None, GroundTruthError]:
        """No-op for Go indexer DB."""
        return Ok(None)

    # --- Property and Assertion queries (v16 schema) ---

    def get_properties(self, node_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        """Get properties for a node. Optionally filter by kind."""
        if not self.connection:
            return []
        try:
            if kind:
                cursor = self.connection.execute(
                    "SELECT kind, value, line, confidence FROM properties WHERE node_id = ? AND kind = ?",
                    (node_id, kind),
                )
            else:
                cursor = self.connection.execute(
                    "SELECT kind, value, line, confidence FROM properties WHERE node_id = ?",
                    (node_id,),
                )
            return [
                {"kind": row[0], "value": row[1], "line": row[2], "confidence": row[3]}
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError:
            return []  # Table may not exist in older DBs

    def get_assertions(self, test_node_id: int) -> list[dict[str, Any]]:
        """Get assertions for a test function node."""
        if not self.connection:
            return []
        try:
            cursor = self.connection.execute(
                "SELECT kind, expression, expected, line FROM assertions WHERE test_node_id = ?",
                (test_node_id,),
            )
            return [
                {"kind": row[0], "expression": row[1], "expected": row[2], "line": row[3]}
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError:
            return []

    def get_assertions_for_target(self, target_name: str) -> list[dict[str, Any]]:
        """Get assertions that test a specific function (by matching expression text)."""
        if not self.connection:
            return []
        try:
            cursor = self.connection.execute(
                "SELECT a.kind, a.expression, a.expected, a.line, n.name as test_name, n.file_path "
                "FROM assertions a JOIN nodes n ON a.test_node_id = n.id "
                "WHERE a.expression LIKE ?",
                (f"%{target_name}%",),
            )
            return [
                {
                    "kind": row[0],
                    "expression": row[1],
                    "expected": row[2],
                    "line": row[3],
                    "test_name": row[4],
                    "file_path": row[5],
                }
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError:
            return []

    def get_property_counts(self) -> dict[str, int]:
        """Get count of properties by kind."""
        if not self.connection:
            return {}
        try:
            cursor = self.connection.execute("SELECT kind, COUNT(*) FROM properties GROUP BY kind")
            return {row[0]: row[1] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return {}

    def get_assertion_count(self) -> int:
        """Get total number of assertions."""
        if not self.connection:
            return 0
        try:
            cursor = self.connection.execute("SELECT COUNT(*) FROM assertions")
            row = cursor.fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0

    # --- Structural queries for evidence parity (v16+) ---

    def get_functions_in_file(self, file_path: str) -> list[dict[str, Any]]:
        """Get all function/method nodes in a file with their properties."""
        if not self.connection:
            return []
        try:
            norm = self._normalize_path(file_path)
            cursor = self.connection.execute(
                "SELECT id, name, label, start_line, end_line, signature, return_type, is_test, language "
                "FROM nodes WHERE file_path = ? AND label IN ('Function', 'Method') "
                "ORDER BY start_line",
                (norm,),
            )
            results = []
            for row in cursor.fetchall():
                results.append(
                    {
                        "id": row[0],
                        "name": row[1],
                        "label": row[2],
                        "start_line": row[3],
                        "end_line": row[4],
                        "signature": row[5],
                        "return_type": row[6],
                        "is_test": bool(row[7]),
                        "language": row[8],
                    }
                )
            return results
        except sqlite3.OperationalError:
            return []

    def get_sibling_functions(self, node_id: int) -> list[dict[str, Any]]:
        """Get sibling functions (same file or same parent class) with properties.

        Returns all function/method nodes that share the same parent_id or file_path,
        excluding the node itself. Each result includes pre-fetched properties.
        """
        if not self.connection:
            return []
        try:
            # Find the target node's file and parent
            cursor = self.connection.execute(
                "SELECT file_path, parent_id FROM nodes WHERE id = ?", (node_id,)
            )
            row = cursor.fetchone()
            if not row:
                return []
            file_path, parent_id = row[0], row[1]

            # Get siblings: same parent (if class) or same file (if top-level)
            if parent_id and parent_id > 0:
                cursor = self.connection.execute(
                    "SELECT id, name, label, start_line, end_line, signature, return_type "
                    "FROM nodes WHERE parent_id = ? AND id != ? AND label IN ('Function', 'Method') "
                    "ORDER BY start_line",
                    (parent_id, node_id),
                )
            else:
                cursor = self.connection.execute(
                    "SELECT id, name, label, start_line, end_line, signature, return_type "
                    "FROM nodes WHERE file_path = ? AND id != ? AND label IN ('Function', 'Method') "
                    "AND (parent_id IS NULL OR parent_id = 0) "
                    "ORDER BY start_line",
                    (file_path, node_id),
                )

            siblings = []
            for r in cursor.fetchall():
                sib = {
                    "id": r[0],
                    "name": r[1],
                    "label": r[2],
                    "start_line": r[3],
                    "end_line": r[4],
                    "signature": r[5],
                    "return_type": r[6],
                    "properties": self.get_properties(r[0]),
                }
                siblings.append(sib)
            return siblings
        except sqlite3.OperationalError:
            return []

    def get_assertions_in_file(self, file_path: str) -> list[dict[str, Any]]:
        """Get all assertions from test functions in a file."""
        if not self.connection:
            return []
        try:
            norm = self._normalize_path(file_path)
            cursor = self.connection.execute(
                "SELECT a.kind, a.expression, a.expected, a.line, n.name as test_name "
                "FROM assertions a JOIN nodes n ON a.test_node_id = n.id "
                "WHERE n.file_path = ? ORDER BY a.line",
                (norm,),
            )
            return [
                {
                    "kind": row[0],
                    "expression": row[1],
                    "expected": row[2],
                    "line": row[3],
                    "test_name": row[4],
                }
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError:
            return []

    def get_function_at_line(self, file_path: str, line: int) -> dict[str, Any] | None:
        """Find the function/method node containing a specific line."""
        if not self.connection:
            return None
        try:
            norm = self._normalize_path(file_path)
            cursor = self.connection.execute(
                "SELECT id, name, label, start_line, end_line, signature, return_type, language "
                "FROM nodes WHERE file_path = ? AND start_line <= ? AND end_line >= ? "
                "AND label IN ('Function', 'Method') "
                "ORDER BY (end_line - start_line) ASC LIMIT 1",
                (norm, line, line),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "name": row[1],
                "label": row[2],
                "start_line": row[3],
                "end_line": row[4],
                "signature": row[5],
                "return_type": row[6],
                "language": row[7],
            }
        except sqlite3.OperationalError:
            return None

    # --- Write operations are no-ops for the bridge ---

    _READ_ONLY_ERR = GroundTruthError(
        code="read_only", message="GraphStore is read-only (Go indexer owns writes)"
    )

    def insert_symbol(  # type: ignore[override]
        self,
        name: str = "",
        kind: str = "",
        language: str = "",
        file_path: str = "",
        line_number: int | None = None,
        end_line: int | None = None,
        is_exported: bool = False,
        signature: str | None = None,
        params: str | None = None,
        return_type: str | None = None,
        documentation: str | None = None,
        last_indexed_at: int = 0,
    ) -> Result[int, GroundTruthError]:
        return Err(self._READ_ONLY_ERR)

    def delete_symbols_in_file(self, file_path: str) -> Result[int, GroundTruthError]:
        return Err(self._READ_ONLY_ERR)

    def update_usage_count(self, symbol_id: int, count: int) -> Result[None, GroundTruthError]:
        return Err(self._READ_ONLY_ERR)

    def rebuild_fts(self) -> Result[None, GroundTruthError]:
        return Ok(None)  # No FTS in Go schema
