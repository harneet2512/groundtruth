"""Schema definitions for the representation substrate.

These tables coexist alongside the existing GT schema. They are created
only when the foundation feature flag is enabled or explicitly requested.
"""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
-- Multi-representation storage: multiple independent representations per symbol
CREATE TABLE IF NOT EXISTS symbol_representations (
    symbol_id INTEGER NOT NULL,
    rep_type TEXT NOT NULL,
    rep_version TEXT NOT NULL,
    rep_blob BLOB NOT NULL,
    dim INTEGER,
    source_hash TEXT NOT NULL,
    index_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (symbol_id, rep_type, rep_version)
);

CREATE INDEX IF NOT EXISTS idx_repr_type
ON symbol_representations(rep_type, rep_version);

CREATE INDEX IF NOT EXISTS idx_repr_symbol
ON symbol_representations(symbol_id);

-- Metadata for filtered similarity queries
CREATE TABLE IF NOT EXISTS symbol_similarity_metadata (
    symbol_id INTEGER PRIMARY KEY,
    symbol_kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    module_path TEXT,
    class_name TEXT,
    language TEXT NOT NULL,
    arity INTEGER,
    is_test INTEGER NOT NULL DEFAULT 0,
    inheritance_root TEXT,
    local_scope_key TEXT
);

-- Index version tracking for snapshot consistency
CREATE TABLE IF NOT EXISTS index_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    file_count INTEGER,
    symbol_count INTEGER,
    representation_count INTEGER,
    status TEXT NOT NULL DEFAULT 'building'
);
"""


def create_representation_schema(conn: sqlite3.Connection) -> None:
    """Create the representation tables if they don't exist.

    Safe to call multiple times — all statements use IF NOT EXISTS.
    """
    conn.executescript(SCHEMA_SQL)


def has_representation_schema(conn: sqlite3.Connection) -> bool:
    """Check if the representation schema has been created."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='symbol_representations'"
    )
    return cursor.fetchone() is not None
