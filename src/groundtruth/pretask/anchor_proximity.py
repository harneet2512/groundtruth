"""Anchor proximity (convergence bonus) for v7.4 brief.

Files reached by multiple distinct trusted anchors within 1 hop get
a bonus: anchor_prox = min(1.0, n_anchors_within_1_hop / 3.0).

This rewards files where multiple entry points converge — a structural
signal that the file is load-bearing for the issue, not a coincidental
graph neighbor.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict


def compute_anchor_proximity(
    trusted_anchors: list[str],
    graph_db: str,
) -> dict[str, float]:
    """Return {file_path: anchor_prox_score} for all 1-hop neighbors of trusted anchors."""
    if not trusted_anchors or not graph_db:
        return {}

    conn = sqlite3.connect(graph_db)
    c = conn.cursor()

    # Count distinct trusted anchors that can reach each file in ≤1 hop
    neighbor_count: dict[str, set[str]] = defaultdict(set)

    # Self: each anchor is reachable from itself (0 hops)
    for anchor in trusted_anchors:
        neighbor_count[anchor].add(anchor)

    # 1-hop neighbors
    placeholders = ",".join("?" * len(trusted_anchors))
    c.execute(
        f"""
        SELECT DISTINCT n1.file_path AS src_file, n2.file_path AS dst_file
        FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE n1.file_path IN ({placeholders})
          AND n2.file_path IS NOT NULL
          AND n1.file_path != n2.file_path
          AND COALESCE(e.confidence, 0.5) >= 0.7
        """,
        trusted_anchors,
    )
    for src, dst in c.fetchall():
        neighbor_count[dst].add(src)

    conn.close()

    return {
        fp: min(1.0, len(anchors) / 3.0)
        for fp, anchors in neighbor_count.items()
    }
