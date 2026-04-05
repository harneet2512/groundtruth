"""
Ego-graph retrieval for GT structural navigation.

Given seed entities (from issue keywords or edited functions), BFS through
graph.db edges and return a structural neighborhood as a readable map.

Three independent hard caps prevent BFS explosion:
  1. Fan-out: max 10 neighbors per node per direction (SQL LIMIT)
  2. Total nodes: BFS stops at 30 visited nodes
  3. Output lines: format_structural_map() emits max 8 lines

Adaptive edge filtering per language:
  - If LSP edges exist for a language: traverse only verified edges
  - If no LSP edges: traverse all edges (degraded but functional)
"""

from __future__ import annotations

import os
import sqlite3
from collections import deque
from typing import Any


# Hard caps
MAX_FANOUT = 10       # neighbors per node per direction
MAX_NODES = 30        # total BFS visited nodes
MAX_OUTPUT_LINES = 8  # structural map lines


def get_edge_filter(file_path: str, conn: sqlite3.Connection) -> str:
    """
    Determine edge filter SQL for a file's language.

    If LSP edges exist for this extension → only traverse verified edges.
    If no LSP edges → traverse all edges (degraded mode).
    """
    ext = os.path.splitext(file_path)[1].lower()
    if not ext:
        return "1=1"

    has_lsp = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution_method = 'lsp' "
        "AND source_id IN (SELECT id FROM nodes WHERE file_path LIKE ?)",
        (f"%{ext}",),
    ).fetchone()[0] > 0

    if has_lsp:
        return "e.resolution_method IN ('lsp', 'import', 'same_file')"
    else:
        return "1=1"


def extract_ego_graph(
    seed_node_ids: list[int],
    conn: sqlite3.Connection,
    max_hops: int = 3,
) -> list[dict[str, Any]]:
    """
    Extract the structural neighborhood around seed entities.

    BFS from seed nodes through edges table. Only traverses edges matching
    the adaptive edge filter (verified-only if LSP is available for the language).

    Args:
        seed_node_ids: Node IDs from graph.db to start BFS from.
        conn: SQLite connection to graph.db.
        max_hops: BFS depth limit.

    Returns:
        List of edge dicts with from/to/confidence/method/hops.
        Empty list = GT stays silent.
    """
    if not seed_node_ids:
        return []

    # Determine edge filter from the first seed's file path
    seed_file = conn.execute(
        "SELECT file_path FROM nodes WHERE id = ?", (seed_node_ids[0],)
    ).fetchone()
    edge_filter = get_edge_filter(seed_file[0], conn) if seed_file else "1=1"

    visited: set[int] = set()
    edges_found: list[dict[str, Any]] = []

    # BFS frontier: (node_id, hops)
    frontier: deque[tuple[int, int]] = deque()
    for nid in seed_node_ids:
        frontier.append((nid, 0))
        visited.add(nid)

    while frontier:
        if len(visited) >= MAX_NODES:
            break

        current_id, hops = frontier.popleft()
        if hops >= max_hops:
            continue

        # Get current node info
        current = conn.execute(
            "SELECT name, file_path, start_line FROM nodes WHERE id = ?",
            (current_id,),
        ).fetchone()
        if not current:
            continue
        cur_name, cur_file, cur_line = current

        # Outgoing edges (this node calls →)
        outgoing = conn.execute(
            f"SELECT e.target_id, t.name, t.file_path, t.start_line, "
            f"e.confidence, e.resolution_method "
            f"FROM edges e JOIN nodes t ON e.target_id = t.id "
            f"WHERE e.source_id = ? AND {edge_filter} "
            f"ORDER BY e.confidence DESC LIMIT ?",
            (current_id, MAX_FANOUT),
        ).fetchall()

        for target_id, t_name, t_file, t_line, conf, method in outgoing:
            edges_found.append({
                "from": {"name": cur_name, "file": cur_file, "line": cur_line},
                "to": {"name": t_name, "file": t_file, "line": t_line},
                "confidence": conf,
                "method": method,
                "hops": hops + 1,
            })
            if target_id not in visited and len(visited) < MAX_NODES:
                visited.add(target_id)
                frontier.append((target_id, hops + 1))

        # Incoming edges (← calls this node)
        incoming = conn.execute(
            f"SELECT e.source_id, s.name, s.file_path, s.start_line, "
            f"e.confidence, e.resolution_method "
            f"FROM edges e JOIN nodes s ON e.source_id = s.id "
            f"WHERE e.target_id = ? AND {edge_filter} "
            f"ORDER BY e.confidence DESC LIMIT ?",
            (current_id, MAX_FANOUT),
        ).fetchall()

        for source_id, s_name, s_file, s_line, conf, method in incoming:
            edges_found.append({
                "from": {"name": s_name, "file": s_file, "line": s_line},
                "to": {"name": cur_name, "file": cur_file, "line": cur_line},
                "confidence": conf,
                "method": method,
                "hops": hops + 1,
            })
            if source_id not in visited and len(visited) < MAX_NODES:
                visited.add(source_id)
                frontier.append((source_id, hops + 1))

    return edges_found


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

    # Sort: closest first (hops ASC), most confident first (confidence DESC)
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
    """
    Find node IDs matching seed names in graph.db.

    Args:
        names: Entity names extracted from issue text or edited functions.
        conn: SQLite connection to graph.db.

    Returns:
        List of node IDs. Empty = no matches → GT stays silent.
    """
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
    """
    Find test file that imports or tests any seed entity.

    Uses edges: if a test node has an edge to a seed entity,
    that test file is relevant.

    Returns:
        Test command string like "python -m pytest path/test.py -xvs" or None.
    """
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

    # Detect test framework from file extension
    ext = os.path.splitext(test_file)[1].lower()
    if ext == ".py":
        return f"python -m pytest {test_file} -xvs"
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        return f"npx jest {test_file}"
    elif ext == ".go":
        test_dir = os.path.dirname(test_file) or "."
        return f"go test ./{test_dir}/..."
    elif ext == ".rs":
        return f"cargo test"
    elif ext == ".java":
        return f"mvn test"
    elif ext == ".rb":
        return f"bundle exec rspec {test_file}"

    return None
