"""Stage A/B graph reachability for v7.4 brief.

BFS from trusted anchors up to max_depth hops, traversing graph.db edges.
Each reachable file gets a reach score that decays with path length and
scales with edge type weight × confidence.

Edge type weights (language-agnostic):
  CALLS    = 1.0
  USES     = 0.8
  IMPORTS  = 0.6
  CONTAINS = 0.4
  INHERITS = 0.4
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

# Edge type → reach weight (hand-set, not per-language)
EDGE_TYPE_WEIGHT: dict[str, float] = {
    "CALLS": 1.0,
    "USES": 0.8,
    "IMPORTS": 0.6,
    "CONTAINS": 0.4,
    "INHERITS": 0.4,
}
_DEFAULT_EDGE_WEIGHT = 0.3  # for unknown edge types


def _edge_weight(edge_type: str) -> float:
    return EDGE_TYPE_WEIGHT.get(edge_type.upper(), _DEFAULT_EDGE_WEIGHT)


@dataclass
class ReachRecord:
    path: str
    reach_score: float
    min_path_length: int  # shortest hop count from any trusted anchor
    entered_via_graph: bool  # True if admitted via graph expansion


def _build_file_graph(
    graph_db: str,
    *,
    min_confidence: float = 0.0,
) -> dict[str, list[tuple[str, str, float]]]:
    """Return adjacency: {src_file: [(dst_file, edge_type, confidence), ...]}."""
    conn = sqlite3.connect(graph_db)
    c = conn.cursor()
    c.execute(
        """
        SELECT n1.file_path, n2.file_path, e.type, COALESCE(e.confidence, 0.5)
        FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE n1.file_path IS NOT NULL
          AND n2.file_path IS NOT NULL
          AND n1.file_path != n2.file_path
          AND COALESCE(e.confidence, 0.5) >= ?
        """,
        (min_confidence,),
    )
    rows = c.fetchall()
    conn.close()
    adj: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for src, dst, etype, conf in rows:
        adj[src].append((dst, etype, float(conf)))
    return dict(adj)


def compute_reach(
    trusted_anchors: list[str],
    graph_db: str,
    *,
    max_depth: int = 3,
    min_confidence: float = 0.0,
    hub_penalties: dict[str, float] | None = None,
) -> dict[str, ReachRecord]:
    """BFS from trusted_anchors up to max_depth hops.

    Reach score accumulates over all paths:
        contribution = prod(weight_i * conf_i) * 1/(1 + path_length)

    v7.5 H2 — path-specificity weighting:
        When hub_penalties is provided, each edge contribution is multiplied by
        (1 - hub_pen(cur_file)) before accumulating into the path product. Paths
        that traverse hub intermediate nodes contribute less reach to downstream
        files — hub-driven reach reflects graph centrality, not issue relevance.
        Basis: Lao & Cohen 2010 (Path Ranking Algorithm) discriminative-path principle.

    Returns {file_path: ReachRecord} for all reachable files
    (excluding the anchor files themselves, which are in the candidate set
    via semantic_top_k already).
    """
    if not trusted_anchors or not graph_db:
        return {}

    adj = _build_file_graph(graph_db, min_confidence=min_confidence)
    _hub = hub_penalties or {}

    # BFS state: {file: (min_depth, cumulative_reach_score)}
    reach: dict[str, list] = {}  # file -> [min_depth, total_reach]
    anchor_set = set(trusted_anchors)

    # BFS queue: (current_file, depth, path_edge_product)
    from collections import deque
    queue: deque[tuple[str, int, float]] = deque()

    for anchor in trusted_anchors:
        queue.append((anchor, 0, 1.0))
        if anchor not in reach:
            reach[anchor] = [0, 0.0]

    while queue:
        cur_file, depth, path_product = queue.popleft()

        if depth >= max_depth:
            continue

        # v7.5 H2: discount edge contributions from hub intermediate nodes.
        # This makes paths through high-centrality files less informative.
        path_spec = max(0.0, 1.0 - _hub.get(cur_file, 0.0)) if _hub else 1.0

        for dst_file, etype, conf in adj.get(cur_file, []):
            edge_contrib = _edge_weight(etype) * conf * path_spec
            new_product = path_product * edge_contrib
            new_depth = depth + 1
            reach_contribution = new_product * (1.0 / (1.0 + new_depth))

            if dst_file not in reach:
                reach[dst_file] = [new_depth, 0.0]
            else:
                if new_depth < reach[dst_file][0]:
                    reach[dst_file][0] = new_depth

            reach[dst_file][1] += reach_contribution

            # Continue BFS if not yet at max depth
            if new_depth < max_depth:
                queue.append((dst_file, new_depth, new_product))

    result: dict[str, ReachRecord] = {}
    for fp, (min_dep, score) in reach.items():
        result[fp] = ReachRecord(
            path=fp,
            reach_score=score,
            min_path_length=min_dep,
            entered_via_graph=(fp not in anchor_set),
        )

    return result


def graph_expand_candidates(
    trusted_anchors: list[str],
    graph_db: str,
    *,
    max_depth: int = 3,
    min_confidence: float = 0.0,
) -> set[str]:
    """Return the set of files reachable from trusted anchors (Stage A candidate expansion)."""
    reach = compute_reach(
        trusted_anchors, graph_db, max_depth=max_depth, min_confidence=min_confidence
    )
    return set(reach.keys())
