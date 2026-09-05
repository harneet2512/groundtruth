"""Universal path resolver — DOC_OF_HONOR §1.1.

Single source of truth for converting agent-supplied paths into the canonical
`nodes.file_path` stored in graph.db.

Handles:
- absolute paths (`/workspace/instance_id/foo/bar.py`)
- workspace-prefixed paths (`instance_id/foo/bar.py`)
- relative paths (`foo/bar.py`, `./foo/bar.py`)
- Windows-style separators
- suffix-only paths (`bar.py` when only one match exists)

Returns the exact stored path or None if not found. Consumers stay silent on
None instead of returning wrong data — preserves Cursor-style honesty.

Per CLAUDE.md: this is the kind of single-function boundary normalization
required so every consumer query operates on canonical keys instead of
reinventing inline normalization.
"""
from __future__ import annotations

import os
import sqlite3
from functools import lru_cache


def _normalize(path: str) -> str:
    """Convert to forward-slash, strip ./ and leading /."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _strip_workspace(path: str, workspace_root: str) -> str:
    """Strip workspace_root prefix if path starts with it."""
    if not workspace_root:
        return path
    ws = _normalize(workspace_root)
    np = _normalize(path)
    if np.startswith(ws + "/"):
        return np[len(ws) + 1:]
    if np == ws:
        return ""
    return np


def _candidate_forms(agent_path: str, workspace_root: str) -> list[str]:
    """Generate ordered candidate forms most-canonical to least."""
    seen: set[str] = set()
    out: list[str] = []

    def add(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    np = _normalize(agent_path)
    add(np)

    if workspace_root:
        stripped = _strip_workspace(agent_path, workspace_root)
        if stripped:
            add(stripped)

    # Strip common instance-id prefix like `kozea__weasyprint-2300/...`
    parts = np.split("/")
    if len(parts) > 1 and "__" in parts[0]:
        add("/".join(parts[1:]))

    # Strip /workspace/ or /testbed/ container prefixes
    for container in ("workspace/", "testbed/", "repo/"):
        if np.startswith(container):
            tail = np[len(container):]
            add(tail)
            # If next segment looks like instance_id, strip it too
            tparts = tail.split("/")
            if len(tparts) > 1 and "__" in tparts[0]:
                add("/".join(tparts[1:]))

    return out


@lru_cache(maxsize=1024)
def _stored_paths_for_basename(graph_db: str, basename: str) -> tuple[str, ...]:
    """Return all stored paths ending in this basename. Cached for repeated calls."""
    try:
        conn = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA busy_timeout = 3000")
    except sqlite3.Error:
        return ()
    try:
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE ? ESCAPE '\\'",
            (f"%/{basename}",),
        ).fetchall()
        also_root = conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path = ?",
            (basename,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        conn.close()
    return tuple(r[0] for r in rows) + tuple(r[0] for r in also_root)


def resolve_to_stored_path(
    agent_path: str,
    graph_db: str,
    workspace_root: str = "",
) -> str | None:
    """Resolve agent-supplied path to canonical file_path in nodes table.

    Returns the exact stored path or None if not found / ambiguous.

    Resolution order:
      1. Try exact match against each candidate form (most canonical first)
      2. Fall back to basename match only when exactly ONE path ends in
         that basename (avoids LIKE-suffix false positives)

    Args:
        agent_path: path as the agent sees it (absolute, relative, prefixed)
        graph_db: path to graph.db (read-only access)
        workspace_root: optional workspace root for prefix stripping

    Returns:
        Stored canonical path (matches nodes.file_path exactly) or None.

    Note:
        Returns None when ambiguous (multiple files share basename) — consumer
        should stay silent rather than guess. This is the Cursor-style honesty
        principle from CLAUDE.md.
    """
    if not agent_path or not graph_db:
        return None
    if not os.path.exists(graph_db):
        return None

    forms = _candidate_forms(agent_path, workspace_root)
    if not forms:
        return None

    try:
        conn = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA busy_timeout = 3000")
    except sqlite3.Error:
        return None

    try:
        for form in forms:
            row = conn.execute(
                "SELECT file_path FROM nodes WHERE file_path = ? LIMIT 1",
                (form,),
            ).fetchone()
            if row:
                return row[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    basename = os.path.basename(forms[0]) if forms else ""
    if basename and "/" not in basename:
        candidates = _stored_paths_for_basename(graph_db, basename)
        if len(candidates) == 1:
            return candidates[0]

    return None


def resolve_or_none(
    agent_path: str,
    graph_db: str,
    workspace_root: str = "",
) -> str | None:
    """Alias for resolve_to_stored_path — clearer at call sites."""
    return resolve_to_stored_path(agent_path, graph_db, workspace_root)


def is_known(agent_path: str, graph_db: str, workspace_root: str = "") -> bool:
    """True iff the agent path resolves to a stored node path."""
    return resolve_to_stored_path(agent_path, graph_db, workspace_root) is not None


def clear_cache() -> None:
    """Reset the basename cache. Call after L6 reindex."""
    _stored_paths_for_basename.cache_clear()
