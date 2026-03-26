"""Graph expander — BFS over multiple edge types from seed symbols."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field

from groundtruth.index.store import SymbolStore


@dataclass
class ExpansionRule:
    """Defines how to traverse one kind of edge in the symbol graph."""

    edge_type: str  # caller, callee, override_chain, same_class, import_dependents, constructor_pair, shared_state
    direction: str  # outgoing, incoming, both, membership
    max_per_seed: int  # prevent explosion
    weight: float  # ranking weight


@dataclass
class ExpandedNode:
    """A symbol discovered by graph expansion."""

    symbol_id: int
    relation: str  # edge_type that found this
    depth: int
    source_seed: int  # which seed it was expanded from


class GraphExpander:
    """Expand seed symbol IDs through the symbol graph using configurable rules."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    def expand(
        self,
        seed_ids: list[int],
        expansion_rules: list[ExpansionRule],
        max_depth: int = 2,
        max_expanded: int = 50,
    ) -> list[ExpandedNode]:
        """BFS expansion from seeds through multiple edge types.

        Returns discovered nodes ordered by depth then weight (highest first).
        Seed IDs themselves are NOT included in the output.
        """
        # Validate seeds exist
        valid_seeds: set[int] = set()
        for sid in seed_ids:
            row = self._conn.execute(
                "SELECT id FROM symbols WHERE id = ?", (sid,)
            ).fetchone()
            if row is not None:
                valid_seeds.add(sid)

        if not valid_seeds:
            return []

        # BFS state
        visited: set[int] = set(valid_seeds)
        results: list[ExpandedNode] = []

        # Queue entries: (symbol_id, depth, original_seed)
        queue: deque[tuple[int, int, int]] = deque()
        for sid in valid_seeds:
            queue.append((sid, 0, sid))

        while queue and len(results) < max_expanded:
            current_id, depth, origin_seed = queue.popleft()

            if depth >= max_depth:
                continue

            for rule in expansion_rules:
                neighbors = self._apply_rule(current_id, rule)
                added_for_rule = 0

                for neighbor_id in neighbors:
                    if neighbor_id in visited:
                        continue
                    if len(results) >= max_expanded:
                        break
                    if added_for_rule >= rule.max_per_seed:
                        break

                    visited.add(neighbor_id)
                    added_for_rule += 1

                    node = ExpandedNode(
                        symbol_id=neighbor_id,
                        relation=rule.edge_type,
                        depth=depth + 1,
                        source_seed=origin_seed,
                    )
                    results.append(node)
                    queue.append((neighbor_id, depth + 1, origin_seed))

        # Sort by depth ascending, then by weight of the rule that found them (descending)
        rule_weights = {r.edge_type: r.weight for r in expansion_rules}
        results.sort(key=lambda n: (n.depth, -rule_weights.get(n.relation, 0.0)))

        return results

    def _apply_rule(self, symbol_id: int, rule: ExpansionRule) -> list[int]:
        """Apply a single expansion rule and return neighbor symbol IDs."""
        edge = rule.edge_type

        if edge == "caller":
            return self._find_callers(symbol_id)
        elif edge == "callee":
            return self._find_callees(symbol_id)
        elif edge == "same_class":
            return self._find_same_class(symbol_id)
        elif edge == "import_dependents":
            return self._find_import_dependents(symbol_id)
        elif edge == "constructor_pair":
            return self._find_constructor_pair(symbol_id)
        elif edge == "override_chain":
            return self._find_override_chain(symbol_id)
        elif edge == "shared_state":
            return self._find_shared_state(symbol_id)
        else:
            return []

    def _find_callers(self, symbol_id: int) -> list[int]:
        """Find symbols in files that reference this symbol (incoming edges)."""
        try:
            # Get files that reference this symbol
            rows = self._conn.execute(
                "SELECT DISTINCT referenced_in_file FROM refs WHERE symbol_id = ?",
                (symbol_id,),
            ).fetchall()

            caller_ids: list[int] = []
            for row in rows:
                ref_file = row["referenced_in_file"]
                # Get symbols defined in those files
                sym_rows = self._conn.execute(
                    "SELECT id FROM symbols WHERE file_path = ?",
                    (ref_file,),
                ).fetchall()
                for sr in sym_rows:
                    if sr["id"] != symbol_id:
                        caller_ids.append(sr["id"])
            return caller_ids
        except sqlite3.Error:
            return []

    def _find_callees(self, symbol_id: int) -> list[int]:
        """Find symbols referenced from the seed's file (outgoing edges)."""
        try:
            # Get the file of this symbol
            sym_row = self._conn.execute(
                "SELECT file_path FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
            if sym_row is None:
                return []

            file_path = sym_row["file_path"]

            # Find symbols referenced from that file
            rows = self._conn.execute(
                """SELECT DISTINCT r.symbol_id
                   FROM refs r
                   WHERE r.referenced_in_file = ?
                   AND r.symbol_id != ?""",
                (file_path, symbol_id),
            ).fetchall()

            return [row["symbol_id"] for row in rows]
        except sqlite3.Error:
            return []

    def _find_same_class(self, symbol_id: int) -> list[int]:
        """Find sibling symbols in the same file with the same kind (class membership)."""
        try:
            sym_row = self._conn.execute(
                "SELECT file_path, kind FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
            if sym_row is None:
                return []

            file_path = sym_row["file_path"]

            # Get all symbols in the same file (sibling methods/attributes)
            rows = self._conn.execute(
                "SELECT id FROM symbols WHERE file_path = ? AND id != ?",
                (file_path, symbol_id),
            ).fetchall()

            return [row["id"] for row in rows]
        except sqlite3.Error:
            return []

    def _find_import_dependents(self, symbol_id: int) -> list[int]:
        """Find symbols in files that have import refs pointing to the seed's file."""
        try:
            sym_row = self._conn.execute(
                "SELECT file_path FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
            if sym_row is None:
                return []

            file_path = sym_row["file_path"]

            # Files that import symbols from this file
            rows = self._conn.execute(
                """SELECT DISTINCT r.referenced_in_file
                   FROM refs r
                   JOIN symbols s ON r.symbol_id = s.id
                   WHERE s.file_path = ?
                   AND r.referenced_in_file != ?""",
                (file_path, file_path),
            ).fetchall()

            dependent_ids: list[int] = []
            for row in rows:
                dep_file = row["referenced_in_file"]
                sym_rows = self._conn.execute(
                    "SELECT id FROM symbols WHERE file_path = ?",
                    (dep_file,),
                ).fetchall()
                for sr in sym_rows:
                    dependent_ids.append(sr["id"])
            return dependent_ids
        except sqlite3.Error:
            return []

    def _find_constructor_pair(self, symbol_id: int) -> list[int]:
        """Find constructor-paired symbols in the same file.

        If seed is __init__, find __eq__, __repr__, __hash__, to_dict, from_dict,
        serialize, deserialize. If seed is to_dict, find from_dict. Etc.
        """
        CONSTRUCTOR_PAIRS: dict[str, list[str]] = {
            "__init__": [
                "__eq__", "__repr__", "__hash__", "__str__",
                "to_dict", "from_dict", "serialize", "deserialize",
            ],
            "__eq__": ["__hash__", "__init__"],
            "__hash__": ["__eq__", "__init__"],
            "__repr__": ["__str__", "__init__"],
            "__str__": ["__repr__", "__init__"],
            "to_dict": ["from_dict", "__init__"],
            "from_dict": ["to_dict", "__init__"],
            "serialize": ["deserialize", "__init__"],
            "deserialize": ["serialize", "__init__"],
        }

        try:
            sym_row = self._conn.execute(
                "SELECT name, file_path FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
            if sym_row is None:
                return []

            name = sym_row["name"]
            file_path = sym_row["file_path"]

            pair_names = CONSTRUCTOR_PAIRS.get(name)
            if not pair_names:
                return []

            placeholders = ",".join("?" for _ in pair_names)
            rows = self._conn.execute(
                f"SELECT id FROM symbols WHERE file_path = ? AND name IN ({placeholders}) AND id != ?",  # noqa: S608
                (file_path, *pair_names, symbol_id),
            ).fetchall()

            return [row["id"] for row in rows]
        except sqlite3.Error:
            return []

    def _find_override_chain(self, symbol_id: int) -> list[int]:
        """Find symbols with the same name in other files (possible overrides)."""
        try:
            sym_row = self._conn.execute(
                "SELECT name, file_path FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
            if sym_row is None:
                return []

            name = sym_row["name"]
            file_path = sym_row["file_path"]

            rows = self._conn.execute(
                "SELECT id FROM symbols WHERE name = ? AND file_path != ? AND id != ?",
                (name, file_path, symbol_id),
            ).fetchall()

            return [row["id"] for row in rows]
        except sqlite3.Error:
            return []

    def _find_shared_state(self, symbol_id: int) -> list[int]:
        """Find methods that share self.* attributes with this symbol.

        Uses the attributes table: symbol_id is the class, method_ids is a JSON
        array of methods that read/write that attribute.
        """
        try:
            # Get the file of this symbol to find its class
            sym_row = self._conn.execute(
                "SELECT file_path FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
            if sym_row is None:
                return []

            # Find attributes where this symbol_id appears in method_ids
            attr_rows = self._conn.execute(
                "SELECT method_ids FROM attributes"
            ).fetchall()

            co_methods: set[int] = set()
            for attr_row in attr_rows:
                method_ids_json = attr_row["method_ids"]
                if not method_ids_json:
                    continue
                try:
                    method_ids = json.loads(method_ids_json)
                except (json.JSONDecodeError, TypeError):
                    continue

                if not isinstance(method_ids, list):
                    continue

                int_ids = [int(m) for m in method_ids if isinstance(m, (int, float, str))]
                if symbol_id in int_ids:
                    # All other methods sharing this attribute
                    for mid in int_ids:
                        if mid != symbol_id:
                            co_methods.add(mid)

            return list(co_methods)
        except sqlite3.Error:
            return []
