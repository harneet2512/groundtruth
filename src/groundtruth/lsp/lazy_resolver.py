"""
Lazy edge resolution for GT ego-graph BFS.

Called DURING BFS when traversing name-match edges. Asks LSP
whether the edge is real, caches the result in graph.db.

Includes:
- Column accuracy fix: finds callee_name in line text (not raw tree-sitter col)
- Selective verification: skips LSP for high-confidence edges (>= 0.7)
- Batch support: resolve_edges_batch() for depth-batched parallel resolution
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from .session import LSPSession


# Edges with confidence >= this are trusted without LSP verification.
# Single-candidate name-match = 0.9, two candidates = 0.6.
# We only LSP-verify ambiguous edges (multi-candidate).
CONFIDENCE_TRUST_THRESHOLD = 0.7


def should_traverse_edge(
    resolution_method: str,
    confidence: float,
) -> bool | None:
    """
    Quick decision: should we traverse this edge?

    Returns:
        True  — traverse (already verified or high confidence)
        False — skip (already failed)
        None  — needs lazy LSP resolution
    """
    if resolution_method in ("lsp", "import", "same_file"):
        return True
    if resolution_method == "lsp_failed":
        return False
    # name_match: trust high-confidence, resolve low-confidence
    if resolution_method == "name_match" and confidence >= CONFIDENCE_TRUST_THRESHOLD:
        return True
    # Low-confidence name_match: needs LSP
    return None


def find_accurate_column(
    file_path: str,
    line_1indexed: int,
    callee_name: str,
    workspace_root: str,
) -> int:
    """
    Find the accurate column of callee_name in the source line.

    Tree-sitter gives the column of the call_expression root (e.g., `self`
    in `self.foo.bar.baz()`). LSP needs the column of the actual callee
    (`baz`). We find the last occurrence of callee_name before the `(`.

    Args:
        file_path: Relative path from workspace root.
        line_1indexed: 1-indexed line number from graph.db.
        callee_name: The function/method name being called.
        workspace_root: Absolute path to workspace root.

    Returns:
        0-indexed column of callee_name, or 0 if not found.
    """
    abs_path = os.path.join(workspace_root, file_path)
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if line_1indexed <= 0 or line_1indexed > len(lines):
            return 0
        line_text = lines[line_1indexed - 1]

        # Find callee_name before the opening paren
        paren_pos = line_text.find("(")
        search_end = paren_pos if paren_pos > 0 else len(line_text)
        col = line_text.rfind(callee_name, 0, search_end)
        if col >= 0:
            return col
        # Fallback: find anywhere in the line
        col = line_text.find(callee_name)
        return col if col >= 0 else 0
    except Exception:
        return 0


def resolve_edge_lazily(
    source_id: int,
    target_id: int,
    source_line: int,
    conn: sqlite3.Connection,
    lsp_session: LSPSession,
) -> bool:
    """
    Resolve a single name-match edge via LSP.

    Called during BFS when a low-confidence name-match edge is encountered.
    Asks LSP, caches result. Returns True (traverse) or False (skip).

    Does NOT write to graph.db — caller batches writes.
    Returns (should_traverse, update_dict_or_none).
    """
    # Look up the call site for exact position
    call_site = conn.execute(
        "SELECT cs.line, cs.col, cs.file_path, cs.callee_name "
        "FROM call_sites cs "
        "WHERE cs.caller_node_id = ? "
        "AND cs.callee_name = (SELECT name FROM nodes WHERE id = ?) "
        "AND cs.line = ? "
        "LIMIT 1",
        (source_id, target_id, source_line),
    ).fetchone()

    if call_site is None:
        return False

    cs_line, cs_col, cs_file, cs_callee = call_site

    # Get LSP client
    client = lsp_session.get_client(cs_file)
    if client is None:
        # No LSP server → traverse name-match in degraded mode
        return True

    # Fix column accuracy: find callee_name in the actual line text
    accurate_col = find_accurate_column(
        cs_file, cs_line, cs_callee, lsp_session.workspace_root
    )

    # Ensure file is open in LSP server
    lsp_session.ensure_file_open(cs_file)

    try:
        # LSP uses 0-indexed lines, call_sites stores 1-indexed
        result = client.goto_definition(cs_file, cs_line - 1, accurate_col)
    except Exception:
        return False

    if result is None:
        return False

    # Check if LSP target matches the expected target node
    target_node = conn.execute(
        "SELECT id FROM nodes "
        "WHERE file_path = ? AND start_line <= ? AND end_line >= ? "
        "AND label IN ('Function', 'Method', 'Class') "
        "ORDER BY (end_line - start_line) ASC LIMIT 1",
        (result["file"], result["line"] + 1, result["line"] + 1),
    ).fetchone()

    if target_node is not None and target_node[0] == target_id:
        return True  # Correct — caller will batch-update to lsp
    elif target_node is not None:
        return False  # Wrong target — name-match was incorrect
    else:
        return False  # Resolved to stdlib/external — not in graph


def resolve_edges_batch(
    edges: list[dict[str, Any]],
    conn: sqlite3.Connection,
    lsp_session: LSPSession,
) -> dict[tuple[int, int], str]:
    """
    Resolve multiple name-match edges via pipelined LSP requests.

    Used for depth-batched resolution: collect all unresolved edges at a
    BFS depth, resolve them all in one batch, then continue BFS.

    Args:
        edges: List of edge dicts with source_id, target_id, source_line.
        conn: graph.db connection.
        lsp_session: Active LSP session.

    Returns:
        Dict mapping (source_id, target_id) → "lsp" or "lsp_failed".
    """
    if not edges:
        return {}

    # Collect call site info and compute accurate columns
    requests: list[tuple[str, int, int]] = []  # (file, line_0idx, col)
    edge_keys: list[tuple[int, int]] = []
    valid_indices: list[int] = []

    for i, edge in enumerate(edges):
        call_site = conn.execute(
            "SELECT cs.line, cs.col, cs.file_path, cs.callee_name "
            "FROM call_sites cs "
            "WHERE cs.caller_node_id = ? "
            "AND cs.callee_name = (SELECT name FROM nodes WHERE id = ?) "
            "AND cs.line = ? "
            "LIMIT 1",
            (edge["source_id"], edge["target_id"], edge.get("source_line", 0)),
        ).fetchone()

        if call_site is None:
            continue

        cs_line, cs_col, cs_file, cs_callee = call_site

        client = lsp_session.get_client(cs_file)
        if client is None:
            continue

        # Fix column accuracy
        accurate_col = find_accurate_column(
            cs_file, cs_line, cs_callee, lsp_session.workspace_root
        )

        lsp_session.ensure_file_open(cs_file)
        requests.append((cs_file, cs_line - 1, accurate_col))
        edge_keys.append((edge["source_id"], edge["target_id"]))
        valid_indices.append(i)

    if not requests:
        return {}

    # Get the client (all requests should be same language for pipelining)
    # Group by language if mixed
    ext = os.path.splitext(requests[0][0])[1].lower()
    client = lsp_session.get_client(f"dummy{ext}")
    if client is None:
        return {}

    # Send pipelined batch
    results = client.batch_goto_definition(requests, timeout_per_request=0.2)

    # Match results to edges
    updates: dict[tuple[int, int], str] = {}
    for idx, (result, key) in enumerate(zip(results, edge_keys)):
        edge = edges[valid_indices[idx]]
        if result is None:
            updates[key] = "lsp_failed"
            continue

        # Check if target matches
        target_node = conn.execute(
            "SELECT id FROM nodes "
            "WHERE file_path = ? AND start_line <= ? AND end_line >= ? "
            "AND label IN ('Function', 'Method', 'Class') "
            "ORDER BY (end_line - start_line) ASC LIMIT 1",
            (result["file"], result["line"] + 1, result["line"] + 1),
        ).fetchone()

        if target_node is not None and target_node[0] == edge["target_id"]:
            updates[key] = "lsp"
        else:
            updates[key] = "lsp_failed"

    return updates
