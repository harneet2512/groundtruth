"""k-hop ego-graph query — structured subgraph centered on a symbol.

RepoGraph (ICLR 2025): k=1 gives 11.6 nodes + 37.1 edges average.
k≥3 adds noise. Ego-graph provides structural context grep cannot:
callers, callees, parent class, siblings, and their relationships.

Usage:
    from groundtruth.graph.ego import ego_graph
    result = ego_graph(db_path, symbol_name, file_path, k=1)
    print(result.render())  # compact token-efficient format
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field


@dataclass
class EgoNode:
    id: int
    name: str
    label: str  # Function, Method, Class, etc.
    file_path: str
    start_line: int = 0
    is_test: bool = False
    hop: int = 0  # distance from center


@dataclass
class EgoEdge:
    source_id: int
    target_id: int
    edge_type: str  # CALLS, IMPORTS, EXTENDS, IMPLEMENTS
    confidence: float = 0.0
    source_line: int = 0


@dataclass
class EgoGraph:
    center: EgoNode | None = None
    nodes: dict[int, EgoNode] = field(default_factory=dict)
    edges: list[EgoEdge] = field(default_factory=list)
    k: int = 1

    @property
    def callers(self) -> list[EgoNode]:
        if not self.center:
            return []
        caller_ids = {e.source_id for e in self.edges
                      if e.target_id == self.center.id and e.edge_type == "CALLS"}
        return [self.nodes[i] for i in caller_ids if i in self.nodes]

    @property
    def callees(self) -> list[EgoNode]:
        if not self.center:
            return []
        callee_ids = {e.target_id for e in self.edges
                      if e.source_id == self.center.id and e.edge_type == "CALLS"}
        return [self.nodes[i] for i in callee_ids if i in self.nodes]

    @property
    def parent_class(self) -> EgoNode | None:
        if not self.center:
            return None
        for e in self.edges:
            if e.source_id == self.center.id and e.edge_type in ("EXTENDS", "IMPLEMENTS"):
                return self.nodes.get(e.target_id)
        return None

    def render(self, max_tokens: int = 150) -> str:
        """Structure-preserving hierarchical rendering.

        Research: RepoScope 2025 — indentation-based hierarchy preserves
        call chain structure. Codebase-Memory 2026 — 10x fewer tokens at
        83% quality via structured graph rendering.

        Format (tree with indentation for hierarchy):
            center_name() in file.py:line
            Called by:
              caller1() file.py:line [test]
              caller2() other.py:line
            Calls:
              callee1() in utils.py
              callee2() in db.py
            Parent: ClassName in file.py
        """
        if not self.center:
            return ""
        parts = [f"{self.center.name}() in {_basename(self.center.file_path)}:{self.center.start_line}"]

        callers = self.callers
        if callers:
            parts.append("Called by:")
            for c in sorted(callers, key=lambda x: (not x.is_test, x.file_path))[:5]:
                tag = " [test]" if c.is_test else ""
                parts.append(f"  {c.name}() {_basename(c.file_path)}:{c.start_line}{tag}")
                # 2-hop: show callers of this caller (transitive impact)
                if self.k >= 2:
                    c_callers = [e for e in self.edges
                                 if e.target_id == c.id and e.edge_type == "CALLS"
                                 and e.source_id != self.center.id]
                    for cc_edge in c_callers[:2]:
                        cc = self.nodes.get(cc_edge.source_id)
                        if cc:
                            cc_tag = " [test]" if cc.is_test else ""
                            parts.append(f"    {cc.name}() {_basename(cc.file_path)}:{cc.start_line}{cc_tag}")

        callees = self.callees
        if callees:
            parts.append("Calls:")
            for c in callees[:5]:
                parts.append(f"  {c.name}() in {_basename(c.file_path)}")

        pc = self.parent_class
        if pc:
            parts.append(f"Parent: {pc.name} in {_basename(pc.file_path)}")

        rendered = "\n".join(parts)
        if len(rendered) > max_tokens * 4:
            last_nl = rendered.rfind("\n", 0, max_tokens * 4)
            rendered = rendered[:last_nl] if last_nl > 0 else rendered[:max_tokens * 4]
        return rendered

    def render_impact(self, changed_function: str = "") -> str:
        """Render change impact analysis (CodePlan FSE 2024 format).

        Shows what breaks if the center function changes:
        transitive callers organized by hop distance.
        """
        if not self.center:
            return ""
        parts = [f"Impact of changing {self.center.name}():"]
        callers_by_hop: dict[int, list[EgoNode]] = {}
        for node in self.nodes.values():
            if node.id != self.center.id and node.hop > 0:
                is_caller = any(e.target_id == self.center.id and e.source_id == node.id
                                for e in self.edges)
                is_transitive = any(
                    e.source_id == node.id and e.edge_type == "CALLS"
                    for e in self.edges
                ) and not is_caller
                if is_caller or is_transitive:
                    callers_by_hop.setdefault(node.hop, []).append(node)
        for hop in sorted(callers_by_hop):
            label = "direct" if hop == 1 else f"{hop}-hop"
            parts.append(f"  {label}:")
            for n in callers_by_hop[hop][:5]:
                tag = " [test]" if n.is_test else ""
                parts.append(f"    {n.name}() in {_basename(n.file_path)}:{n.start_line}{tag}")
        if not callers_by_hop:
            parts.append("  (no cross-file callers found)")
        return "\n".join(parts)


def _basename(path: str) -> str:
    return os.path.basename(path) if path else "?"


def ego_graph(
    db_path: str,
    symbol_name: str,
    file_path: str = "",
    *,
    k: int = 1,
    min_confidence: float = 0.5,
) -> EgoGraph:
    """Build k-hop ego-graph centered on a symbol.

    Args:
        db_path: path to graph.db
        symbol_name: function/method/class name
        file_path: optional file path for disambiguation
        k: number of hops (1 or 2 recommended)
        min_confidence: minimum edge confidence
    """
    result = EgoGraph(k=k)
    if not os.path.isfile(db_path):
        return result

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Find center node
    if file_path:
        suffix = "%" + file_path.replace("\\", "/").lstrip("/")
        rows = conn.execute(
            "SELECT id, name, label, file_path, start_line, is_test FROM nodes "
            "WHERE name = ? AND file_path LIKE ? AND is_test = 0 LIMIT 1",
            (symbol_name, suffix),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, label, file_path, start_line, is_test FROM nodes "
            "WHERE name = ? AND is_test = 0 "
            "ORDER BY (SELECT COUNT(*) FROM edges WHERE target_id = nodes.id) DESC LIMIT 1",
            (symbol_name,),
        ).fetchall()

    if not rows:
        conn.close()
        return result

    center_row = rows[0]
    center = EgoNode(
        id=center_row["id"],
        name=center_row["name"],
        label=center_row["label"],
        file_path=center_row["file_path"],
        start_line=center_row["start_line"] or 0,
        is_test=bool(center_row["is_test"]),
        hop=0,
    )
    result.center = center
    result.nodes[center.id] = center

    # BFS for k hops
    frontier = {center.id}
    for hop in range(1, k + 1):
        next_frontier: set[int] = set()
        for node_id in frontier:
            # Outgoing edges (callees)
            out_edges = conn.execute(
                "SELECT e.id, e.target_id, e.type, e.confidence, e.source_line, "
                "n.id as nid, n.name, n.label, n.file_path, n.start_line, n.is_test "
                "FROM edges e JOIN nodes n ON e.target_id = n.id "
                "WHERE e.source_id = ? AND COALESCE(e.confidence, 0.5) >= ? "
                "LIMIT 10",
                (node_id, min_confidence),
            ).fetchall()
            for row in out_edges:
                tid = row["target_id"]
                if tid not in result.nodes:
                    result.nodes[tid] = EgoNode(
                        id=tid, name=row["name"], label=row["label"],
                        file_path=row["file_path"],
                        start_line=row["start_line"] or 0,
                        is_test=bool(row["is_test"]), hop=hop,
                    )
                    next_frontier.add(tid)
                result.edges.append(EgoEdge(
                    source_id=node_id, target_id=tid,
                    edge_type=row["type"], confidence=row["confidence"] or 0.0,
                    source_line=row["source_line"] or 0,
                ))

            # Incoming edges (callers)
            in_edges = conn.execute(
                "SELECT e.id, e.source_id, e.type, e.confidence, e.source_line, "
                "n.id as nid, n.name, n.label, n.file_path, n.start_line, n.is_test "
                "FROM edges e JOIN nodes n ON e.source_id = n.id "
                "WHERE e.target_id = ? AND COALESCE(e.confidence, 0.5) >= ? "
                "LIMIT 10",
                (node_id, min_confidence),
            ).fetchall()
            for row in in_edges:
                sid = row["source_id"]
                if sid not in result.nodes:
                    result.nodes[sid] = EgoNode(
                        id=sid, name=row["name"], label=row["label"],
                        file_path=row["file_path"],
                        start_line=row["start_line"] or 0,
                        is_test=bool(row["is_test"]), hop=hop,
                    )
                    next_frontier.add(sid)
                result.edges.append(EgoEdge(
                    source_id=sid, target_id=node_id,
                    edge_type=row["type"], confidence=row["confidence"] or 0.0,
                    source_line=row["source_line"] or 0,
                ))

        frontier = next_frontier

    conn.close()
    return result


def change_impact(
    db_path: str,
    changed_function: str,
    file_path: str = "",
    *,
    max_depth: int = 2,
    min_confidence: float = 0.7,
) -> list[dict]:
    """Trace transitive callers impacted by a function change.

    CodePlan (FSE 2024): change-may-impact analysis via CalledBy edges.
    Returns list of impacted functions with hop distance and file path.
    """
    if not os.path.isfile(db_path):
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Find the changed node
    if file_path:
        suffix = "%" + file_path.replace("\\", "/").lstrip("/")
        rows = conn.execute(
            "SELECT id, name, file_path FROM nodes "
            "WHERE name = ? AND file_path LIKE ? AND is_test = 0 LIMIT 1",
            (changed_function, suffix),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, file_path FROM nodes "
            "WHERE name = ? AND is_test = 0 LIMIT 1",
            (changed_function,),
        ).fetchall()

    if not rows:
        conn.close()
        return []

    center_id = rows[0]["id"]
    impacted: list[dict] = []
    visited: set[int] = {center_id}
    frontier = {center_id}

    for depth in range(1, max_depth + 1):
        next_frontier: set[int] = set()
        for node_id in frontier:
            callers = conn.execute(
                "SELECT DISTINCT n.id, n.name, n.file_path, n.start_line, n.is_test, "
                "e.source_line, e.confidence "
                "FROM edges e JOIN nodes n ON e.source_id = n.id "
                "WHERE e.target_id = ? AND e.type = 'CALLS' "
                "AND COALESCE(e.confidence, 0.5) >= ? "
                "AND n.file_path != (SELECT file_path FROM nodes WHERE id = ?) "
                "LIMIT 10",
                (node_id, min_confidence, center_id),
            ).fetchall()
            for c in callers:
                if c["id"] not in visited:
                    visited.add(c["id"])
                    next_frontier.add(c["id"])
                    impacted.append({
                        "name": c["name"],
                        "file": c["file_path"],
                        "line": c["start_line"] or 0,
                        "is_test": bool(c["is_test"]),
                        "hop": depth,
                        "confidence": c["confidence"] or 0.0,
                    })
        frontier = next_frontier

    conn.close()
    return impacted
