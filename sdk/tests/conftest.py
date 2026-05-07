"""SQLite graph fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _minimal_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          label TEXT NOT NULL,
          name TEXT NOT NULL,
          qualified_name TEXT,
          file_path TEXT NOT NULL,
          start_line INTEGER,
          end_line INTEGER,
          signature TEXT,
          return_type TEXT,
          is_exported BOOLEAN DEFAULT 0,
          is_test BOOLEAN DEFAULT 0,
          language TEXT NOT NULL,
          parent_id INTEGER REFERENCES nodes(id)
        );

        CREATE TABLE edges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL REFERENCES nodes(id),
          target_id INTEGER NOT NULL REFERENCES nodes(id),
          type TEXT NOT NULL,
          source_line INTEGER,
          source_file TEXT,
          resolution_method TEXT,
          confidence REAL DEFAULT 0.0,
          metadata TEXT
        );
        """
    )


def _seed_chain_graph(connection: sqlite3.Connection) -> None:
    """Linear call chain: ``top`` -> ``mid`` -> ``leaf`` with mixed resolution methods."""
    connection.executemany(
        """
        INSERT INTO nodes (label, name, qualified_name, file_path, start_line, language)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("Function", "top", "pkg.top", "src/a.py", 1, "python"),
            ("Function", "mid", "pkg.mid", "src/b.py", 2, "python"),
            ("Function", "leaf", "pkg.leaf", "src/c.py", 3, "python"),
        ],
    )
    connection.execute(
        """
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (1, 2, 'CALLS', 10, 'src/a.py', 'import', 1.0)
        """
    )
    connection.execute(
        """
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (2, 3, 'CALLS', 20, 'src/b.py', 'name_match', 0.4)
        """
    )
    # Second caller to ``mid`` (ambiguous + deterministic mix)
    connection.execute(
        """
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
        VALUES (3, 2, 'CALLS', 5, 'src/c.py', 'same_file', 1.0)
        """
    )


@pytest.fixture()
def graph_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "graph.db"
    conn = sqlite3.connect(path)
    try:
        _minimal_schema(conn)
        _seed_chain_graph(conn)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def empty_graph_path(tmp_path: Path) -> Path:
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    try:
        _minimal_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path
