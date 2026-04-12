"""Concrete GraphReader implementation wrapping GraphStore.

This adapter translates between the substrate's GraphReader protocol
(plain dicts, narrow interface) and the existing GraphStore class
(SymbolRecord/RefRecord, Result types, wider surface).
"""

from __future__ import annotations

import sqlite3

from groundtruth.index.graph_store import GraphStore
from groundtruth.utils.result import Err


class GraphStoreReader:
    """Adapts GraphStore to the GraphReader protocol.

    All methods return plain dicts (or None/empty list) rather than
    Result types or SymbolRecord objects — keeping the substrate
    protocol decoupled from the index layer's specific types.
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._conn: sqlite3.Connection = store.connection

    # ------------------------------------------------------------------
    # Node queries
    # ------------------------------------------------------------------

    def get_node_by_name(
        self, name: str, file_path: str | None = None
    ) -> dict | None:
        """Find a node by symbol name, optionally scoped to file."""
        try:
            if file_path:
                cursor = self._conn.execute(
                    "SELECT * FROM nodes WHERE name = ? AND file_path LIKE ?",
                    (name, f"%{file_path}"),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM nodes WHERE name = ?", (name,)
                )
            row = cursor.fetchone()
            return dict(row) if row else None
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
        """Get all nodes that call this node (incoming CALLS edges)."""
        try:
            cursor = self._conn.execute(
                """SELECT e.source_id, e.source_line, e.source_file,
                          e.resolution_method, e.confidence, e.type as edge_type,
                          n.name as source_name, n.file_path as source_file_path,
                          n.start_line as source_start_line
                   FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND e.type = 'CALLS'""",
                (node_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def get_callees(self, node_id: int) -> list[dict]:
        """Get all nodes called by this node (outgoing CALLS edges)."""
        try:
            cursor = self._conn.execute(
                """SELECT e.target_id, e.source_line, e.source_file,
                          e.resolution_method, e.confidence, e.type as edge_type,
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

        Strategy:
        1. Check edges where target_id=node_id and source is a test node
        2. Check assertions table for target_node_id references
        3. Name-match: test_<symbol_name> patterns
        """
        node = self.get_node_by_id(node_id)
        if not node:
            return []

        tests: list[dict] = []
        name = node["name"]

        try:
            # 1. Direct test callers
            cursor = self._conn.execute(
                """SELECT n.* FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND n.is_test = 1""",
                (node_id,),
            )
            tests.extend(dict(row) for row in cursor.fetchall())

            # 2. Tests referencing via assertions
            cursor = self._conn.execute(
                """SELECT DISTINCT n.* FROM assertions a
                   JOIN nodes n ON a.test_node_id = n.id
                   WHERE a.target_node_id = ?""",
                (node_id,),
            )
            seen_ids = {t["id"] for t in tests}
            for row in cursor.fetchall():
                d = dict(row)
                if d["id"] not in seen_ids:
                    tests.append(d)
                    seen_ids.add(d["id"])

            # 3. Name-match: test_<name> or test<Name>
            cursor = self._conn.execute(
                """SELECT * FROM nodes
                   WHERE is_test = 1 AND (name LIKE ? OR name LIKE ?)""",
                (f"test_{name}%", f"test{name}%"),
            )
            for row in cursor.fetchall():
                d = dict(row)
                if d["id"] not in seen_ids:
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
        """Get all nodes defined in a file."""
        try:
            cursor = self._conn.execute(
                "SELECT * FROM nodes WHERE file_path LIKE ?",
                (f"%{file_path}",),
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
