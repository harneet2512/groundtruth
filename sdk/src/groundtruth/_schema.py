"""Validate ``graph.db`` schema at SDK init time."""

from __future__ import annotations

import sqlite3
from typing import FrozenSet

from groundtruth.exceptions import SchemaVersionError

# Minimum tables required for the Go indexer SQLite schema (see CLAUDE.md ``graph.db``).
EXPECTED_TABLES: FrozenSet[str] = frozenset({"nodes", "edges"})
REQUIRED_NODE_COLUMNS: FrozenSet[str] = frozenset(
    {"id", "label", "name", "qualified_name", "file_path", "start_line"}
)
REQUIRED_EDGE_COLUMNS: FrozenSet[str] = frozenset(
    {"id", "source_id", "target_id", "type", "source_line", "source_file"}
)


def validate_schema(conn: sqlite3.Connection) -> None:
    """Raise ``SchemaVersionError`` if required tables are missing.

    If ``PRAGMA user_version`` is non-zero, we treat it as a forward-compatible marker
    for future indexer versions once ``gt-index`` starts setting it; table checks still apply.
    """
    cur = conn.execute("PRAGMA user_version")
    row = cur.fetchone()
    _ = row  # Forward-compatible: gt-index may set user_version later.

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    actual = {r[0] for r in cur.fetchall() if r[0]}
    missing = sorted(EXPECTED_TABLES - actual)
    if missing:
        raise SchemaVersionError(
            f"graph.db schema mismatch. Expected tables: {sorted(EXPECTED_TABLES)}. "
            f"Found: {sorted(actual)}. Missing: {missing}. "
            "Re-index with gt-index >= v1.0.0."
        )

    node_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)").fetchall()}
    node_missing = sorted(REQUIRED_NODE_COLUMNS - node_cols)
    edge_missing = sorted(REQUIRED_EDGE_COLUMNS - edge_cols)
    if node_missing or edge_missing:
        raise SchemaVersionError(
            "graph.db column mismatch.\n"
            f"nodes missing: {node_missing}; edges missing: {edge_missing}. "
            "Re-index with gt-index >= v1.0.0."
        )
