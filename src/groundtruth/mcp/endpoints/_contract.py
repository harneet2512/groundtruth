"""Shared helpers to wire the deterministic Contract reader into MCP endpoints.

Thin adapters over ``groundtruth.pretask.contract_map`` (the existing, tested
deterministic reader). They resolve the graph.db path from the store and emit
the correct-or-quiet contract surface: a function's own raises / guards / return
shape, read from the ``properties`` table. Empty string when nothing is known
or the store is not backed by a graph.db (no properties table).
"""

from __future__ import annotations

from typing import Any

from groundtruth.index.graph_store import is_graph_db


def _db_path(store: Any) -> str | None:
    """Return the graph.db path the store reads, or None when not a graph db."""
    path = getattr(store, "_db_path", None)
    if not path or path == ":memory:":
        return None
    try:
        if is_graph_db(path):
            return str(path)
    except Exception:
        return None
    return None


def contract_block_for(store: Any, file_path: str, func_name: str) -> str:
    """Full ``<gt-contract>`` block for one (file, function), or "" when no signal."""
    db_path = _db_path(store)
    if not db_path or not file_path or not func_name:
        return ""
    try:
        from groundtruth.pretask.contract_map import build_contract, render_contract

        return render_contract(build_contract(db_path, [(file_path, func_name)]))
    except Exception:
        return ""


def contract_line_for(store: Any, file_path: str, func_name: str) -> str:
    """Compact one-line contract for one (file, function), or "" when no signal."""
    db_path = _db_path(store)
    if not db_path or not file_path or not func_name:
        return ""
    try:
        from groundtruth.pretask.contract_map import contract_line

        return contract_line(db_path, file_path, [func_name])
    except Exception:
        return ""
