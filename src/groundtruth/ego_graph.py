"""
Ego-graph retrieval for GT structural navigation.

Given seed entities (from issue keywords or edited functions), BFS through
graph.db edges and return a structural neighborhood as a readable map.

v1.0.1 lazy resolution: name-match edges are verified by LSP on demand
during BFS traversal. Results cached in graph.db permanently.

Three independent hard caps prevent BFS explosion:
  1. Fan-out: max 10 neighbors per node per direction (SQL LIMIT)
  2. Total nodes: BFS stops at 30 visited nodes
  3. Output lines: format_structural_map() emits max 8 lines

Four debate-driven optimizations:
  1. Column accuracy: rfind callee_name in line text before LSP call
  2. Pipelined batch LSP: depth-batched parallel resolution
  3. Skip LSP for confidence >= 0.7 (single-candidate name-match)
  4. Batch writes: accumulate updates, flush one transaction after BFS
"""

from __future__ import annotations

import os
import sqlite3
from collections import deque
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from groundtruth.lsp.session import LSPSession

# Hard caps
MAX_FANOUT = 10       # neighbors per node per direction
MAX_NODES = 30        # total BFS visited nodes
MAX_OUTPUT_LINES = 8  # structural map lines


def extract_ego_graph(
    seed_node_ids: list[int],
    conn: sqlite3.Connection,
    max_hops: int = 3,
    lsp_session: Any | None = None,
    min_confidence: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Extract the structural neighborhood around seed entities.

    BFS from seed nodes through edges table. Name-match edges are lazily
    verified by LSP during traversal (if lsp_session provided).

    Args:
        seed_node_ids: Node IDs from graph.db to start BFS from.
        conn: SQLite connection to graph.db.
        max_hops: BFS depth limit.
        lsp_session: Optional LSPSession for lazy edge resolution.
        min_confidence: Minimum edge confidence to traverse (0.0 = all edges).
            Use 0.7 to traverse only verified edges (same_file, import, scip).

    Returns:
        List of edge dicts with from/to/confidence/method/hops.
        Empty list = GT stays silent.
    """
    if not seed_node_ids:
        return []

    # Import lazy resolver only if LSP session is available
    lazy_resolve = None
    if lsp_session is not None:
        try:
            from groundtruth.lsp.lazy_resolver import (
                should_traverse_edge,
                resolve_edges_batch,
            )
            lazy_resolve = True
        except ImportError:
            lazy_resolve = None

    visited: set[int] = set()
    edges_found: list[dict[str, Any]] = []
    # Batched write cache: (source_id, target_id) → new resolution_method
    write_cache: dict[tuple[int, int], str] = {}

    # BFS frontier: (node_id, hops)
    frontier: deque[tuple[int, int]] = deque()
    for nid in seed_node_ids:
        frontier.append((nid, 0))
        visited.add(nid)

    while frontier:
        if len(visited) >= MAX_NODES:
            break

        # Process all nodes at current depth (for depth-batched LSP)
        current_depth_nodes: list[tuple[int, int]] = []
        peek_hops = frontier[0][1] if frontier else -1

        while frontier and frontier[0][1] == peek_hops:
            current_depth_nodes.append(frontier.popleft())

        # Collect all edges at this depth
        depth_edges: list[dict[str, Any]] = []
        unresolved_at_depth: list[dict[str, Any]] = []

        for current_id, hops in current_depth_nodes:
            if hops >= max_hops:
                continue

            current = conn.execute(
                "SELECT name, file_path, start_line FROM nodes WHERE id = ?",
                (current_id,),
            ).fetchone()
            if not current:
                continue
            cur_name, cur_file, cur_line = current

            # Outgoing edges
            outgoing = conn.execute(
                "SELECT e.source_id, e.target_id, t.name, t.file_path, t.start_line, "
                "e.confidence, e.resolution_method, e.source_line "
                "FROM edges e JOIN nodes t ON e.target_id = t.id "
                "WHERE e.source_id = ? AND e.confidence >= ? "
                "ORDER BY e.confidence DESC LIMIT ?",
                (current_id, min_confidence, MAX_FANOUT),
            ).fetchall()

            for src_id, tgt_id, t_name, t_file, t_line, conf, method, src_line in outgoing:
                # Check write cache first
                cached = write_cache.get((src_id, tgt_id))
                if cached:
                    method = cached
                    conf = 1.0 if cached == "lsp" else 0.1

                edge_info = {
                    "source_id": src_id, "target_id": tgt_id,
                    "source_line": src_line or 0,
                    "from": {"name": cur_name, "file": cur_file, "line": cur_line},
                    "to": {"name": t_name, "file": t_file, "line": t_line},
                    "confidence": conf, "method": method, "hops": hops + 1,
                    "neighbor_id": tgt_id,
                }

                if lazy_resolve:
                    decision = should_traverse_edge(method, conf)
                    if decision is True:
                        depth_edges.append(edge_info)
                    elif decision is None:
                        unresolved_at_depth.append(edge_info)
                    # False → skip
                else:
                    # No LSP: traverse all edges
                    depth_edges.append(edge_info)

            # Incoming edges
            incoming = conn.execute(
                "SELECT e.source_id, e.target_id, s.name, s.file_path, s.start_line, "
                "e.confidence, e.resolution_method, e.source_line "
                "FROM edges e JOIN nodes s ON e.source_id = s.id "
                "WHERE e.target_id = ? AND e.confidence >= ? "
                "ORDER BY e.confidence DESC LIMIT ?",
                (current_id, min_confidence, MAX_FANOUT),
            ).fetchall()

            for src_id, tgt_id, s_name, s_file, s_line, conf, method, edge_src_line in incoming:
                cached = write_cache.get((src_id, tgt_id))
                if cached:
                    method = cached
                    conf = 1.0 if cached == "lsp" else 0.1

                edge_info = {
                    "source_id": src_id, "target_id": tgt_id,
                    "source_line": edge_src_line or 0,
                    "from": {"name": s_name, "file": s_file, "line": s_line},
                    "to": {"name": cur_name, "file": cur_file, "line": cur_line},
                    "confidence": conf, "method": method, "hops": hops + 1,
                    "neighbor_id": src_id,
                }

                if lazy_resolve:
                    decision = should_traverse_edge(method, conf)
                    if decision is True:
                        depth_edges.append(edge_info)
                    elif decision is None:
                        unresolved_at_depth.append(edge_info)
                else:
                    depth_edges.append(edge_info)

        # Depth-batched lazy resolution for unresolved edges
        if unresolved_at_depth and lsp_session is not None:
            batch_results = resolve_edges_batch(unresolved_at_depth, conn, lsp_session)
            for edge_info in unresolved_at_depth:
                key = (edge_info["source_id"], edge_info["target_id"])
                result = batch_results.get(key)
                if result == "lsp":
                    edge_info["method"] = "lsp"
                    edge_info["confidence"] = 1.0
                    depth_edges.append(edge_info)
                    write_cache[key] = "lsp"
                elif result == "lsp_failed":
                    write_cache[key] = "lsp_failed"
                # else: no result, skip edge

        # Add verified edges to results and expand frontier
        for edge_info in depth_edges:
            edges_found.append({
                "from": edge_info["from"],
                "to": edge_info["to"],
                "confidence": edge_info["confidence"],
                "method": edge_info["method"],
                "hops": edge_info["hops"],
            })
            neighbor_id = edge_info["neighbor_id"]
            if neighbor_id not in visited and len(visited) < MAX_NODES:
                visited.add(neighbor_id)
                frontier.append((neighbor_id, edge_info["hops"]))

    # Fix 4: Batch writes — flush all cached resolutions in one transaction
    if write_cache:
        _flush_write_cache(conn, write_cache)

    return edges_found


def _flush_write_cache(
    conn: sqlite3.Connection,
    cache: dict[tuple[int, int], str],
) -> None:
    """Flush accumulated edge resolution updates in a single transaction."""
    try:
        conn.execute("BEGIN")
        for (source_id, target_id), method in cache.items():
            confidence = 1.0 if method == "lsp" else 0.1
            conn.execute(
                "UPDATE edges SET resolution_method = ?, confidence = ? "
                "WHERE source_id = ? AND target_id = ?",
                (method, confidence, source_id, target_id),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


def format_structural_map(ego_edges: list[dict[str, Any]], max_lines: int = MAX_OUTPUT_LINES) -> str | None:
    """
    Render ego-graph as a readable structural map.

    The agent reads this and reasons about which file to explore.
    GT does NOT say "edit this file." GT shows the structural neighborhood.

    Returns:
        Formatted string like "STRUCTURAL MAP:\\n  ..." or None if empty.
    """
    if not ego_edges:
        return None

    lines: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()

    sorted_edges = sorted(ego_edges, key=lambda e: (e["hops"], -e["confidence"]))

    for edge in sorted_edges:
        if len(lines) >= max_lines:
            break

        f = edge["from"]
        t = edge["to"]
        key = (f["name"], f["file"], t["name"], t["file"])
        if key in seen:
            continue
        seen.add(key)

        if f["file"] != t["file"]:
            lines.append(
                f"  {f['name']}() in {f['file']}:{f['line']} "
                f"\u2192 {t['name']}() in {t['file']}:{t['line']}"
            )
        else:
            lines.append(
                f"  {f['name']}() \u2192 {t['name']}() [same file: {f['file']}]"
            )

    if not lines:
        return None

    return "STRUCTURAL MAP:\n" + "\n".join(lines)


def find_seeds_by_name(
    names: list[str],
    conn: sqlite3.Connection,
) -> list[int]:
    """Find node IDs matching seed names in graph.db."""
    node_ids: list[int] = []
    for name in names:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE name = ? "
            "AND label IN ('Function', 'Method', 'Class') "
            "ORDER BY start_line LIMIT 5",
            (name,),
        ).fetchall()
        node_ids.extend(row[0] for row in rows)
    return node_ids


def find_test_for_seeds(
    seed_node_ids: list[int],
    conn: sqlite3.Connection,
) -> str | None:
    """Find test file that imports or tests any seed entity."""
    if not seed_node_ids:
        return None

    placeholders = ",".join("?" * len(seed_node_ids))
    test_row = conn.execute(
        f"SELECT DISTINCT n.file_path FROM nodes n "
        f"JOIN edges e ON e.source_id = n.id "
        f"WHERE e.target_id IN ({placeholders}) "
        f"AND n.is_test = 1 "
        f"LIMIT 1",
        seed_node_ids,
    ).fetchone()

    if not test_row:
        return None

    test_file = test_row[0]
    ext = os.path.splitext(test_file)[1].lower()
    if ext == ".py":
        return f"python -m pytest {test_file} -xvs"
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        return f"npx jest {test_file}"
    elif ext == ".go":
        test_dir = os.path.dirname(test_file) or "."
        return f"go test ./{test_dir}/..."
    elif ext == ".rs":
        return "cargo test"
    elif ext == ".java":
        return "mvn test"
    elif ext == ".rb":
        return f"bundle exec rspec {test_file}"
    return None
