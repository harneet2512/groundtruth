"""Shared fixtures for pretask v5 tests.

We build a tiny in-memory-style graph.db with the Go-indexer schema so
unit tests can exercise the modules without an actual indexed repo.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tiny_graph_db(tmp_path: Path) -> str:
    """Create a minimal Go-indexer-shaped graph.db.

    Layout (3 files, 5 nodes, 4 edges):
        - patroni/watchdog.py: ``SafeWatchdog`` class, ``_fd`` method
        - patroni/postmaster.py: ``Postmaster`` class
        - tests/test_watchdog.py: ``test_watchdog_fires``
        Edges: Postmaster -> SafeWatchdog (CALLS, conf 0.9),
               test_watchdog_fires -> SafeWatchdog (CALLS, conf 0.9),
               SafeWatchdog -> _fd (CALLS, conf 1.0),
               unrelated edge with conf 0.2 (must be filtered out).
    """
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
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
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        """
    )
    rows = [
        # id, label, name, qual, file, start, end, sig, ret, exp, test, lang, parent
        (1, "Class",    "SafeWatchdog",        None, "patroni/watchdog.py",      10, 80,  None, None, 1, 0, "python", None),
        (2, "Method",   "_fd",                 None, "patroni/watchdog.py",      30, 45,  None, None, 0, 0, "python", 1),
        (3, "Class",    "Postmaster",          None, "patroni/postmaster.py",    5,  120, None, None, 1, 0, "python", None),
        (4, "Function", "test_watchdog_fires", None, "tests/test_watchdog.py",   1,  20,  None, None, 0, 1, "python", None),
        (5, "Function", "format_value",        None, "patroni/utils.py",         1,  10,  None, None, 1, 0, "python", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )

    # Edges with confidence:
    edges = [
        # src -> dst, conf
        (3, 1, 100, "patroni/postmaster.py", 0.9),    # Postmaster -> SafeWatchdog
        (4, 1, 5,   "tests/test_watchdog.py", 0.9),   # test_* -> SafeWatchdog
        (1, 2, 35,  "patroni/watchdog.py", 1.0),      # SafeWatchdog -> _fd
        (5, 2, 5,   "patroni/utils.py", 0.2),         # noisy edge, must be filtered
    ]
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, "
        "source_file, resolution_method, confidence) VALUES "
        "(?, ?, 'CALLS', ?, ?, 'name_match', ?)",
        edges,
    )
    conn.commit()
    conn.close()
    return str(db_path)
