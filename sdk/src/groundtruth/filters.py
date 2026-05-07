"""Resolution-method filtering (binary deterministic policy)."""

from __future__ import annotations

from typing import Any, List

DETERMINISTIC_METHODS: frozenset[str] = frozenset({"import", "fqn", "same_file", "class_hierarchy"})


def is_deterministic(method: str) -> bool:
    return method in DETERMINISTIC_METHODS


def filter_edges(edges: List[dict[str, Any]], *, deterministic_only: bool = False) -> List[dict[str, Any]]:
    """Filter edges by resolution method.

    If ``deterministic_only`` is True, keep only deterministic resolution methods.

    If ``deterministic_only`` is False, prefer deterministic edges: if the deterministic
    subset is non-empty, return it; otherwise return all edges (name_match fallback).
    """
    if not edges:
        return []

    def _method(e: dict[str, Any]) -> str:
        m = e.get("resolution_method")
        if m is None or m == "":
            return "name_match"
        return str(m)

    det = [e for e in edges if is_deterministic(_method(e))]
    if deterministic_only:
        return det
    return det if det else list(edges)
