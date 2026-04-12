"""Concrete GraphReader implementation wrapping GraphStore.

This adapter translates between the substrate's GraphReader protocol
(plain dicts, narrow interface) and the existing GraphStore class
(SymbolRecord/RefRecord, Result types, wider surface).

Resolution correctness principle:
- Exact matching preferred over LIKE
- Ambiguous symbol bindings → return None (abstain)
- Name-only test matches tagged for downstream filtering
- Path normalization before comparison
"""

from __future__ import annotations

import logging
import sqlite3

from groundtruth.index.graph_store import GraphStore
from groundtruth.utils.result import Err

logger = logging.getLogger(__name__)

# Minimum edge confidence for obligation-grade evidence
MIN_OBLIGATION_CONFIDENCE = 0.5


class GraphStoreReader:
    """Adapts GraphStore to the GraphReader protocol.

    All methods return plain dicts (or None/empty list) rather than
    Result types or SymbolRecord objects — keeping the substrate
    protocol decoupled from the index layer's specific types.
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._conn: sqlite3.Connection = store.connection
        self._has_edge_confidence = self._detect_edge_confidence()

    # ------------------------------------------------------------------
    # Node queries
    # ------------------------------------------------------------------

    def get_node_by_name(
        self, name: str, file_path: str | None = None
    ) -> dict | None:
        """Find a node by symbol name, with resolution correctness.

        Resolution policy:
        - If file_path given: exact match first, suffix fallback only if no exact
        - If no file_path: return ONLY if exactly 1 match exists
        - If ambiguous (>1 match, no file scope): return None + log ambiguity

        This prevents wrong-symbol binding (audit issue #1).
        """
        try:
            if file_path:
                # Strategy 1: Exact file match
                normalized = self._normalize_path(file_path)
                cursor = self._conn.execute(
                    "SELECT * FROM nodes WHERE name = ? AND file_path = ?",
                    (name, normalized),
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)

                # Strategy 2: Suffix match (for relative paths)
                cursor = self._conn.execute(
                    "SELECT * FROM nodes WHERE name = ? AND file_path LIKE ?",
                    (name, f"%/{file_path.lstrip('./')}"),
                )
                rows = cursor.fetchall()
                if len(rows) == 1:
                    return dict(rows[0])
                if len(rows) > 1:
                    logger.info(
                        "[GT_RESOLUTION] ambiguous: '%s' in '%s' has %d matches — abstaining",
                        name, file_path, len(rows),
                    )
                    return None
            else:
                # No file scope: require unique match
                cursor = self._conn.execute(
                    "SELECT * FROM nodes WHERE name = ?", (name,)
                )
                rows = cursor.fetchall()
                if len(rows) == 1:
                    return dict(rows[0])
                if len(rows) > 1:
                    logger.info(
                        "[GT_RESOLUTION] ambiguous: '%s' has %d matches (no file scope) — abstaining",
                        name, len(rows),
                    )
                    return None
            return None
        except sqlite3.Error:
            return None

    def get_node_by_id(self, node_id: int) -> dict | None:
        """Get a single node by primary key."""
        try:
            cursor = self._conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error:
            return None

    # ------------------------------------------------------------------
    # Edge / relationship queries
    # ------------------------------------------------------------------

    def get_callers(self, node_id: int) -> list[dict]:
        """Get all nodes that call this node (incoming CALLS edges).

        Only returns edges with confidence >= MIN_OBLIGATION_CONFIDENCE
        to prevent cross-file contamination from weak name_match edges
        (audit issue #9).
        """
        try:
            confidence_expr = "e.confidence" if self._has_edge_confidence else "1.0"
            confidence_filter = (
                f"AND e.confidence >= {MIN_OBLIGATION_CONFIDENCE}"
                if self._has_edge_confidence else ""
            )
            cursor = self._conn.execute(
                f"""SELECT e.source_id, e.source_line, e.source_file,
                          e.resolution_method, {confidence_expr} as confidence,
                          e.type as edge_type,
                          n.name as source_name, n.file_path as source_file_path,
                          n.start_line as source_start_line
                   FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND e.type = 'CALLS'
                   {confidence_filter}""",
                (node_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def get_callees(self, node_id: int) -> list[dict]:
        """Get all nodes called by this node (outgoing CALLS edges)."""
        try:
            confidence_expr = "e.confidence" if self._has_edge_confidence else "1.0"
            cursor = self._conn.execute(
                f"""SELECT e.target_id, e.source_line, e.source_file,
                          e.resolution_method, {confidence_expr} as confidence,
                          e.type as edge_type,
                          n.name as target_name, n.file_path as target_file_path
                   FROM edges e
                   JOIN nodes n ON e.target_id = n.id
                   WHERE e.source_id = ? AND e.type = 'CALLS'""",
                (node_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def get_siblings(self, node_id: int) -> list[dict]:
        """Get sibling functions/methods (same parent or file)."""
        results = self._store.get_sibling_functions(node_id)
        return results  # Already returns list[dict]

    def get_tests_for(self, node_id: int) -> list[dict]:
        """Get test nodes that exercise this symbol.

        Resolution policy (audit issue #3):
        - Strategy 1 (call graph): VERIFIED — edge exists in graph
        - Strategy 2 (assertions): VERIFIED — assertion target_node_id
        - Strategy 3 (name match): NAMING_ONLY — tagged, not used for contracts

        Each returned dict includes 'resolution' field for downstream filtering.
        """
        node = self.get_node_by_id(node_id)
        if not node:
            return []

        tests: list[dict] = []
        name = node["name"]

        try:
            # Strategy 1: Direct test callers (VERIFIED)
            cursor = self._conn.execute(
                """SELECT n.* FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND n.is_test = 1""",
                (node_id,),
            )
            for row in cursor.fetchall():
                d = dict(row)
                d["_resolution"] = "call_graph"
                tests.append(d)

            seen_ids = {t["id"] for t in tests}

            # Strategy 2: Tests referencing via assertions (VERIFIED)
            cursor = self._conn.execute(
                """SELECT DISTINCT n.* FROM assertions a
                   JOIN nodes n ON a.test_node_id = n.id
                   WHERE a.target_node_id = ?""",
                (node_id,),
            )
            for row in cursor.fetchall():
                d = dict(row)
                if d["id"] not in seen_ids:
                    d["_resolution"] = "assertion_target"
                    tests.append(d)
                    seen_ids.add(d["id"])

            # Strategy 3: Name-match (NAMING_ONLY — not for contracts)
            cursor = self._conn.execute(
                """SELECT * FROM nodes
                   WHERE is_test = 1 AND (name LIKE ? OR name LIKE ?)""",
                (f"test_{name}", f"test_{name}_%"),  # Exact prefix, not open suffix
            )
            for row in cursor.fetchall():
                d = dict(row)
                if d["id"] not in seen_ids:
                    d["_resolution"] = "naming_only"
                    tests.append(d)
                    seen_ids.add(d["id"])

        except sqlite3.Error:
            pass

        return tests

    # ------------------------------------------------------------------
    # Properties and assertions
    # ------------------------------------------------------------------

    def get_properties(
        self, node_id: int, kind: str | None = None
    ) -> list[dict]:
        """Get structural properties for a node."""
        return self._store.get_properties(node_id, kind)

    def get_assertions(self, test_node_id: int) -> list[dict]:
        """Get assertions for a specific test function."""
        return self._store.get_assertions(test_node_id)

    def get_assertions_for_target(self, target_name: str) -> list[dict]:
        """Get all assertions that reference a target symbol by name."""
        return self._store.get_assertions_for_target(target_name)

    # ------------------------------------------------------------------
    # File-level queries
    # ------------------------------------------------------------------

    def get_nodes_in_file(self, file_path: str) -> list[dict]:
        """Get all nodes defined in a file.

        Uses exact match first, suffix fallback only if needed (audit issue #2).
        """
        try:
            normalized = self._normalize_path(file_path)

            # Exact match first
            cursor = self._conn.execute(
                "SELECT * FROM nodes WHERE file_path = ?",
                (normalized,),
            )
            rows = cursor.fetchall()
            if rows:
                return [dict(row) for row in rows]

            # Suffix fallback for relative paths
            cursor = self._conn.execute(
                "SELECT * FROM nodes WHERE file_path LIKE ?",
                (f"%/{file_path.lstrip('./')}",),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def get_file_paths(self) -> list[str]:
        """Get all indexed file paths."""
        result = self._store.get_all_files()
        if isinstance(result, Err):
            return []
        return result.value

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _normalize_path(self, path: str) -> str:
        """Normalize a file path for comparison."""
        return path.replace("\\", "/").lstrip("./")

    def _detect_edge_confidence(self) -> bool:
        """Return True when the edges table exposes a confidence column."""
        try:
            cursor = self._conn.execute("PRAGMA table_info(edges)")
            return any(row[1] == "confidence" for row in cursor.fetchall())
        except sqlite3.Error:
            return False
