"""Read-only SQLite access to ``graph.db`` (Go indexer schema)."""

from __future__ import annotations

import os
import sqlite3
from collections import deque
from typing import Any, Literal, Mapping, MutableSet, Sequence

from groundtruth._schema import validate_schema
from groundtruth.filters import filter_edges

_EgoDirection = Literal["callers", "callees", "both"]


def _row_to_mapping(cur: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


def _normalize_resolution(raw: Any) -> str:
    if raw is None or raw == "":
        return "name_match"
    s = str(raw).strip().lower()
    if s in {"import", "same_file", "name_match", "fqn", "class_hierarchy"}:
        return s
    # Indexer may emit variant strings; map common ones.
    if s in {"same-file"}:
        return "same_file"
    return "name_match"


class GraphStore:
    """SQLite read layer for ``nodes`` / ``edges`` (see CLAUDE.md ``graph.db`` schema)."""

    def __init__(self, db_path: str, *, read_only: bool | None = None) -> None:
        self._db_path = db_path
        ro = True if read_only is None else read_only
        if db_path == ":memory:":
            ro = False
        if ro:
            abs_path = os.path.abspath(db_path)
            self._conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
        else:
            self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        validate_schema(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a read-only SQL query; returns rows as dicts."""
        cur = self._conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        return [_row_to_mapping(cur, r) for r in rows]

    def find_symbol(self, name: str) -> list[dict[str, Any]]:
        """Return node rows matching ``name`` (exact or substring on ``qualified_name``)."""
        like = f"%{name}%"
        sql = """
        SELECT id, label, name, qualified_name, file_path, start_line, end_line, language
        FROM nodes
        WHERE name = ? OR qualified_name = ? OR qualified_name LIKE ? OR name LIKE ?
        ORDER BY id
        """
        return self.query(sql, (name, name, like, like))

    def symbols_in_file(self, file: str) -> list[dict[str, Any]]:
        """All symbol nodes defined in ``file`` (``file_path`` match)."""
        sql = """
        SELECT id, label, name, qualified_name, file_path, start_line, end_line, language
        FROM nodes
        WHERE file_path = ?
        ORDER BY CASE WHEN start_line IS NULL THEN 1 ELSE 0 END, start_line, id
        """
        return self.query(sql, (file,))

    def callers_of(self, symbol_id: int) -> list[dict[str, Any]]:
        """Incoming ``CALLS`` edges: who calls this symbol."""
        sql = """
        SELECT
          e.id AS edge_id,
          e.source_id,
          e.target_id,
          e.source_line,
          e.source_file,
          e.resolution_method,
          e.confidence,
          sn.name AS caller_name,
          sn.qualified_name AS caller_qualified_name,
          sn.file_path AS caller_file
        FROM edges e
        JOIN nodes sn ON sn.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS'
        """
        rows = self.query(sql, (symbol_id,))
        for r in rows:
            r["resolution_method"] = _normalize_resolution(r.get("resolution_method"))
        return rows

    def callees_of(self, symbol_id: int) -> list[dict[str, Any]]:
        """Outgoing ``CALLS`` edges: symbols this symbol calls."""
        sql = """
        SELECT
          e.id AS edge_id,
          e.source_id,
          e.target_id,
          e.source_line,
          e.source_file,
          e.resolution_method,
          e.confidence,
          tn.name AS callee_name,
          tn.qualified_name AS callee_qualified_name,
          tn.file_path AS callee_file,
          tn.start_line AS callee_line
        FROM edges e
        JOIN nodes tn ON tn.id = e.target_id
        WHERE e.source_id = ? AND e.type = 'CALLS'
        """
        rows = self.query(sql, (symbol_id,))
        for r in rows:
            r["resolution_method"] = _normalize_resolution(r.get("resolution_method"))
        return rows

    def _symbol_label(self, node: Mapping[str, Any]) -> str:
        q = node.get("qualified_name")
        if q:
            return str(q)
        return str(node["name"])

    def ego(
        self,
        symbol: str,
        depth: int = 2,
        *,
        deterministic_only: bool = True,
        direction: _EgoDirection = "callers",
    ) -> list[str]:
        """BFS over call graph within ``depth`` hops from nodes matching ``symbol``.

        ``direction``: traverse incoming calls (callers), outgoing calls (callees), or both.
        Returns sorted unique human-readable symbol labels for all visited nodes.
        """
        starts = self.find_symbol(symbol)
        if not starts:
            return []

        start_ids = {int(s["id"]) for s in starts}
        visited: MutableSet[int] = set()
        out_labels: MutableSet[str] = set()

        def _label_for_id(nid: int) -> None:
            row = self.query(
                "SELECT name, qualified_name FROM nodes WHERE id = ?",
                (nid,),
            )
            if row:
                out_labels.add(self._symbol_label(row[0]))

        queue: deque[tuple[int, int]] = deque()
        for sid in start_ids:
            visited.add(sid)
            _label_for_id(sid)
            queue.append((sid, 0))

        while queue:
            cur_id, d = queue.popleft()
            if d >= depth:
                continue

            next_ids: list[int] = []
            if direction in ("callers", "both"):
                for e in filter_edges(self.callers_of(cur_id), deterministic_only=deterministic_only):
                    next_ids.append(int(e["source_id"]))
            if direction in ("callees", "both"):
                for e in filter_edges(self.callees_of(cur_id), deterministic_only=deterministic_only):
                    next_ids.append(int(e["target_id"]))

            for nid in next_ids:
                if nid in visited:
                    continue
                visited.add(nid)
                _label_for_id(nid)
                queue.append((nid, d + 1))

        return sorted(out_labels)
