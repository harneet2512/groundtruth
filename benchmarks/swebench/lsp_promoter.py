"""LSP promotion wrapper for SWE-agent hook.

Thin sync layer around resolve._get_ambiguous_edges + _resolve_edges.
Scopes resolution to specific source files (checkpoint-relevant),
caches per-task to avoid redundant LSP calls, and returns stats for telemetry.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from typing import Any

# Ensure groundtruth package is importable (fallback paths inside Docker)
for _p in ["/tmp", os.path.dirname(os.path.abspath(__file__))]:
    if _p and _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

# Module-level cache: keyed by edge id -> already resolved in this task.
# Fresh per Docker container (each SWE-bench task = fresh process).
_LSP_CACHE: set[int] = set()


def promote_ambiguous_edges(
    source_files: list[str],
    db_path: str = "/tmp/gt_graph.db",
    root: str = ".",
    language: str = "python",
) -> dict[str, Any]:
    """Promote ambiguous edges in scope files via LSP.

    Returns stats dict: {attempts, verified, corrected, deleted, failed, skipped, cached}.
    On any error returns {error: str}.
    """
    if not source_files:
        return {"attempts": 0, "skipped": 0, "reason": "no_files"}

    if not os.path.exists(db_path):
        return {"attempts": 0, "error": "no_db"}

    try:
        from groundtruth.resolve import _get_ambiguous_edges, _resolve_edges
    except ImportError:
        return {"attempts": 0, "error": "resolve_import_failed"}

    conn = sqlite3.connect(db_path, timeout=5)
    try:
        edges = _get_ambiguous_edges(
            conn,
            min_confidence=0.9,
            language=language,
            source_files=source_files,
        )
    finally:
        conn.close()

    if not edges:
        return {"attempts": 0, "skipped": 0, "reason": "no_ambiguous_edges"}

    # Filter out already-resolved edges (cache hit)
    cached = 0
    fresh_edges = []
    for e in edges:
        eid = e["id"]
        if eid in _LSP_CACHE:
            cached += 1
        else:
            fresh_edges.append(e)

    if not fresh_edges:
        return {"attempts": len(edges), "cached": cached, "skipped": 0}

    # Run async resolution synchronously
    try:
        stats = asyncio.run(
            _resolve_edges(db_path, root, fresh_edges, language)
        )
    except Exception as e:
        return {"attempts": len(fresh_edges), "error": str(e)[:200]}

    # Mark resolved edges as cached
    for e in fresh_edges:
        _LSP_CACHE.add(e["id"])

    stats["attempts"] = len(fresh_edges)
    stats["cached"] = cached
    return stats
