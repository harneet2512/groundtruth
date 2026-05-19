"""Layer 4 graph-edge providers.

Pure functions that query ``graph.db`` for structural relationships of a file:
who calls into it, where it calls out to, who imports it, what its top
functions are, and how dense its in-degree is.

These are extracted verbatim from the SQL inside
``src/groundtruth/hooks/post_view.py::graph_navigation``. The behavior is
preserved byte-for-byte by the parity tests.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerEdge:
    """A file that calls into ``target_file``."""

    file_path: str
    count: int


@dataclass(frozen=True)
class CalleeEdge:
    """A file that ``target_file`` calls into."""

    file_path: str
    count: int


@dataclass(frozen=True)
class ImporterEdge:
    """A file that imports from ``target_file``."""

    file_path: str


@dataclass(frozen=True)
class FunctionInfo:
    """A function defined in a given file, with its reference count."""

    name: str
    ref_count: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _open_readonly(db_path: str) -> sqlite3.Connection | None:
    """Open a SQLite db in read-only mode if possible. Returns ``None`` on any
    failure (mirrors the existing tolerant behavior in post_view).
    """
    if not os.path.isfile(db_path):
        return None
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        try:
            return sqlite3.connect(db_path)
        except sqlite3.Error:
            return None


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").lstrip("./").lstrip("/")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def caller_provider(
    db_path: str,
    file_path: str,
    *,
    min_confidence: float = 0.5,
    limit: int = 5,
) -> list[CallerEdge]:
    """Files that call into ``file_path`` (CALLS edges, conf >= min_confidence).

    Returns up to ``limit`` rows ordered by edge count descending. Excludes
    same-file callers (the existing graph_navigation behavior).
    """
    conn = _open_readonly(db_path)
    if conn is None:
        return []
    needle = _normalize_path(file_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT nsrc.file_path, COUNT(*) AS cnt
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND COALESCE(e.confidence, 0.5) >= ?
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ?
              AND nsrc.file_path != ?
            GROUP BY nsrc.file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (min_confidence, needle, needle, limit),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [CallerEdge(file_path=r[0], count=r[1]) for r in rows]


def callee_provider(
    db_path: str,
    file_path: str,
    *,
    min_confidence: float = 0.5,
    limit: int = 5,
) -> list[CalleeEdge]:
    """Files ``file_path`` calls into (outgoing CALLS edges, conf >= min)."""
    conn = _open_readonly(db_path)
    if conn is None:
        return []
    needle = _normalize_path(file_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT nt.file_path, COUNT(*) AS cnt
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
              AND COALESCE(e.confidence, 0.5) >= ?
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ?
              AND nt.file_path != ?
            GROUP BY nt.file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (min_confidence, needle, needle, limit),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [CalleeEdge(file_path=r[0], count=r[1]) for r in rows]


def importer_provider(
    db_path: str,
    file_path: str,
    *,
    limit: int = 5,
) -> list[ImporterEdge]:
    """Files that import from ``file_path`` (IMPORTS edges, no confidence floor).

    IMPORTS edges currently have no confidence column writes; the conservative
    default is "if it exists, surface it".
    """
    conn = _open_readonly(db_path)
    if conn is None:
        return []
    needle = _normalize_path(file_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT nsrc.file_path
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'IMPORTS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ?
              AND nsrc.file_path != ?
            LIMIT ?
            """,
            (needle, needle, limit),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [ImporterEdge(file_path=r[0]) for r in rows]


def top_functions_provider(
    db_path: str,
    file_path: str,
    *,
    limit: int = 2,
) -> list[FunctionInfo]:
    """Top Function/Method nodes in ``file_path`` by incoming reference count."""
    conn = _open_readonly(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT n.name, COUNT(e.id) AS ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
              AND n.is_test = 0
            GROUP BY n.id
            ORDER BY ref_count DESC, n.name
            LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [FunctionInfo(name=r[0], ref_count=r[1] or 0) for r in rows]


def in_degree_provider(db_path: str, file_path: str) -> int:
    """Total CALLS in-degree for ``file_path`` (used by hub-penalty scoring)."""
    conn = _open_readonly(db_path)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM edges e
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nt.file_path = ?
              AND e.type = 'CALLS'
            """,
            (file_path,),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def hub_scale_provider(db_path: str) -> int:
    """p90 of per-file CALLS in-degree across the repo.

    Used by graph_navigation as a repo-relative hub scale (Decision 22 Fix 1).
    Returns ``50`` when the repo has no CALLS edges, matching the legacy
    fallback.
    """
    conn = _open_readonly(db_path)
    if conn is None:
        return 50
    try:
        all_degrees = [
            r[0]
            for r in conn.execute(
                """
                SELECT COUNT(e.id) FROM nodes n
                JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'
                GROUP BY n.file_path
                ORDER BY 1
                """
            ).fetchall()
        ]
    except sqlite3.Error:
        all_degrees = []
    finally:
        conn.close()
    if not all_degrees:
        return 50
    return all_degrees[int(len(all_degrees) * 0.9)]


__all__ = [
    "CalleeEdge",
    "CallerEdge",
    "FunctionInfo",
    "ImporterEdge",
    "callee_provider",
    "caller_provider",
    "hub_scale_provider",
    "importer_provider",
    "in_degree_provider",
    "top_functions_provider",
]
