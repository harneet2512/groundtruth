"""Error Chain Tracing -- cross-file raise/catch chain evidence.

Uses regex to extract `raise <ExceptionType>` and `except <ExceptionType>` from
Python source, then traces through graph.db CALLS edges to build cross-file chains.

Feature flag: GT_ERROR_CHAIN_ENABLED (env var, default "0" = OFF).
When OFF, all public functions return empty results.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any


def _is_enabled() -> bool:
    return os.environ.get("GT_ERROR_CHAIN_ENABLED", "0") == "1"


def extract_error_surface(source: str) -> list[dict[str, Any]]:
    """Extract raises/catches from Python source.

    Returns list of dicts with keys:
      - kind: "raise" or "catch"
      - exception_type: str (e.g. "ValueError", "Exception")
      - line: int (1-based line number)

    Returns empty list when feature is disabled.
    """
    if not _is_enabled():
        return []

    results: list[dict[str, Any]] = []
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()

        # raise ExceptionType or raise ExceptionType(...)
        m = re.match(r"raise\s+(\w+)", stripped)
        if m:
            exc_type = m.group(1)
            # Skip bare 're-raise' (just 'raise' with no type)
            results.append({"kind": "raise", "exception_type": exc_type, "line": i})
            continue

        # except ExceptionType or except (TypeA, TypeB)
        m = re.match(r"except\s+\(([^)]+)\)", stripped)
        if m:
            for exc in m.group(1).split(","):
                exc_name = exc.strip().split()[0]  # handle 'except (A, B) as e'
                if exc_name:
                    results.append({"kind": "catch", "exception_type": exc_name, "line": i})
            continue

        m = re.match(r"except\s+(\w+)", stripped)
        if m:
            results.append({"kind": "catch", "exception_type": m.group(1), "line": i})
            continue

        # Bare except
        if re.match(r"except\s*:", stripped):
            results.append({"kind": "catch", "exception_type": "bare_except", "line": i})

    return results


def trace_error_chain(
    db_path: str, file_path: str, func_name: str, *, max_hops: int = 2
) -> list[str]:
    """Build cross-file error chain evidence. Max 2-hop depth.

    Traces CALLS edges from graph.db to find:
    - Functions that this function calls, and what they raise
    - Functions that call this function, and what they catch

    Returns list of human-readable chain descriptions like:
      "process_item() RAISES ValueError -> handle_batch() CATCHES ValueError"

    Returns empty list when feature is disabled or for non-Python files.
    """
    if not _is_enabled():
        return []

    # Only Python for now
    if not file_path.endswith(".py"):
        return []

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        return _build_chains(conn, file_path, func_name, max_hops)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _normalize_path(path: str) -> str:
    """Normalize file path for comparison."""
    return path.replace("\\", "/").lstrip("./")


def _find_node(conn: sqlite3.Connection, file_path: str, func_name: str) -> int | None:
    """Find node ID for a function in graph.db."""
    # Try exact match first
    cursor = conn.execute(
        "SELECT id FROM nodes WHERE name = ? AND file_path = ?",
        (func_name, file_path),
    )
    row = cursor.fetchone()
    if row:
        return row["id"]

    # Try normalized (forward-slash) suffix match
    norm = _normalize_path(file_path)
    cursor = conn.execute(
        "SELECT id FROM nodes WHERE name = ? AND file_path LIKE ?",
        (func_name, f"%{norm}"),
    )
    row = cursor.fetchone()
    if row:
        return row["id"]

    # Try with backslashes preserved (Windows paths stored as-is)
    if "\\" in file_path or "/" in file_path:
        # Extract just the filename for a looser match
        basename = os.path.basename(file_path)
        cursor = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND file_path LIKE ?",
            (func_name, f"%{basename}"),
        )
        row = cursor.fetchone()
        return row["id"] if row else None

    return None


def _get_source_for_file(conn: sqlite3.Connection, file_path: str, repo_root: str = "") -> str:
    """Read source for a file path, trying repo_root prefix."""
    candidates = [file_path]
    if repo_root:
        candidates.append(os.path.join(repo_root, file_path))

    for path in candidates:
        try:
            with open(path, "r", errors="replace") as f:
                return f.read()
        except OSError:
            continue
    return ""


def _get_callees(conn: sqlite3.Connection, node_id: int) -> list[dict[str, Any]]:
    """Get functions called by this node (outgoing CALLS edges)."""
    cursor = conn.execute(
        """SELECT n.id, n.name, n.file_path
           FROM edges e
           JOIN nodes n ON e.target_id = n.id
           WHERE e.source_id = ? AND e.type = 'CALLS'""",
        (node_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _get_callers(conn: sqlite3.Connection, node_id: int) -> list[dict[str, Any]]:
    """Get functions that call this node (incoming CALLS edges)."""
    cursor = conn.execute(
        """SELECT n.id, n.name, n.file_path, e.source_file
           FROM edges e
           JOIN nodes n ON e.source_id = n.id
           WHERE e.target_id = ? AND e.type = 'CALLS'""",
        (node_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _build_chains(
    conn: sqlite3.Connection,
    file_path: str,
    func_name: str,
    max_hops: int,
) -> list[str]:
    """Build error chain descriptions."""
    node_id = _find_node(conn, file_path, func_name)
    if node_id is None:
        return []

    chains: list[str] = []

    # Get error surface for the target function itself
    # We need the source file to extract raises/catches
    norm = _normalize_path(file_path)
    cursor = conn.execute(
        "SELECT start_line, end_line, file_path FROM nodes WHERE id = ?",
        (node_id,),
    )
    target_row = cursor.fetchone()
    if not target_row:
        return []

    # --- Forward chain: what do our callees raise? ---
    callees = _get_callees(conn, node_id)
    for callee in callees[:10]:  # limit for performance
        callee_source = _get_source_for_file(conn, callee["file_path"])
        if not callee_source:
            continue
        callee_errors = extract_error_surface.__wrapped__(callee_source) if hasattr(extract_error_surface, '__wrapped__') else _extract_error_surface_raw(callee_source)
        for err in callee_errors:
            if err["kind"] == "raise":
                chains.append(
                    f"{callee['name']}() RAISES {err['exception_type']} -> "
                    f"{func_name}() must handle {err['exception_type']}"
                )

        # 2-hop: what do callee's callees raise?
        if max_hops >= 2:
            callee_node_id = callee["id"]
            sub_callees = _get_callees(conn, callee_node_id)
            for sub in sub_callees[:5]:
                sub_source = _get_source_for_file(conn, sub["file_path"])
                if not sub_source:
                    continue
                sub_errors = _extract_error_surface_raw(sub_source)
                for err in sub_errors:
                    if err["kind"] == "raise":
                        chains.append(
                            f"{sub['name']}() RAISES {err['exception_type']} -> "
                            f"{callee['name']}() -> {func_name}() (2-hop)"
                        )

    # --- Backward chain: what do our callers catch? ---
    callers = _get_callers(conn, node_id)
    # Get our own raises
    own_source = _get_source_for_file(conn, target_row["file_path"])
    own_errors = _extract_error_surface_raw(own_source) if own_source else []
    own_raises = {e["exception_type"] for e in own_errors if e["kind"] == "raise"}

    for caller in callers[:10]:
        caller_file = caller.get("source_file") or caller["file_path"]
        caller_source = _get_source_for_file(conn, caller_file)
        if not caller_source:
            continue
        caller_errors = _extract_error_surface_raw(caller_source)
        caller_catches = {e["exception_type"] for e in caller_errors if e["kind"] == "catch"}

        for raised in own_raises:
            if raised in caller_catches:
                chains.append(
                    f"{func_name}() RAISES {raised} -> "
                    f"{caller['name']}() CATCHES {raised}"
                )
            elif "Exception" in caller_catches or "bare_except" in caller_catches:
                chains.append(
                    f"{func_name}() RAISES {raised} -> "
                    f"{caller['name']}() CATCHES broadly (Exception/bare)"
                )

    return chains


def _extract_error_surface_raw(source: str) -> list[dict[str, Any]]:
    """Internal extraction that bypasses the feature flag (for chain building)."""
    results: list[dict[str, Any]] = []
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()

        m = re.match(r"raise\s+(\w+)", stripped)
        if m:
            results.append({"kind": "raise", "exception_type": m.group(1), "line": i})
            continue

        m = re.match(r"except\s+\(([^)]+)\)", stripped)
        if m:
            for exc in m.group(1).split(","):
                exc_name = exc.strip().split()[0]
                if exc_name:
                    results.append({"kind": "catch", "exception_type": exc_name, "line": i})
            continue

        m = re.match(r"except\s+(\w+)", stripped)
        if m:
            results.append({"kind": "catch", "exception_type": m.group(1), "line": i})
            continue

        if re.match(r"except\s*:", stripped):
            results.append({"kind": "catch", "exception_type": "bare_except", "line": i})

    return results
