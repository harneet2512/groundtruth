"""Module 3 — Personalized PageRank over graph.db.

Standard PPR with the restart vector concentrated on the seed nodes
produced by Modules 1+2. No global PageRank fallback: with no seeds, this
returns ``{}`` and the renderer will abstain.

Implementation:
    - Read edges from graph.db, filtered by ``confidence >= 0.5`` (only
      when the edges table actually has a ``confidence`` column — older
      schemas are accepted unfiltered).
    - Build a column-stochastic adjacency dict-of-dicts. scipy.sparse is
      available in the project but the dict-of-dicts approach scales fine
      to ~100k-edge graphs and avoids a hard dep.
    - Iterate ``v_{t+1} = (1 - alpha) * r + alpha * P @ v_t`` until the
      l-infinity delta drops under ``1e-4`` or ``iterations`` is reached.

Returns per-node scores. The orchestrator aggregates by file.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class PPRResult:
    """Output of one PPR run.

    Attributes:
        node_scores: Final score per node id (only nodes that received
            non-zero mass during iteration are present).
        iterations_run: Number of power-iteration steps actually executed
            (less than ``iterations`` if convergence was hit early).
        converged: True if max-delta dropped below tolerance.
    """

    node_scores: dict[int, float]
    iterations_run: int
    converged: bool


def _has_confidence_column(conn: sqlite3.Connection) -> bool:
    """Edges-table compatibility check (v14+ adds ``confidence``)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        return "confidence" in cols
    except sqlite3.Error:
        return False


def _build_adjacency(
    conn: sqlite3.Connection,
    min_confidence: float,
) -> tuple[dict[int, dict[int, float]], dict[int, int]]:
    """Build forward adjacency and per-node out-degree.

    Returns:
        adj: ``adj[src][dst] = weight`` (weights are 1.0 here — confidence
            is used only as a *gate*, not a transition multiplier, to match
            the spec's reference to standard PPR).
        out_deg: ``out_deg[src] = number of outgoing edges`` for the
            column-stochastic normalization step.
    """
    if _has_confidence_column(conn):
        cursor = conn.execute(
            "SELECT source_id, target_id FROM edges WHERE confidence >= ?",
            (min_confidence,),
        )
    else:
        cursor = conn.execute("SELECT source_id, target_id FROM edges")

    adj: dict[int, dict[int, float]] = defaultdict(dict)
    out_deg: dict[int, int] = defaultdict(int)
    seen_pairs: set[tuple[int, int]] = set()

    for src, dst in cursor.fetchall():
        if src is None or dst is None or src == dst:
            continue
        key = (src, dst)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        adj[src][dst] = 1.0
        out_deg[src] += 1

    return adj, out_deg


def personalized_pagerank(
    graph_db: str,
    seed_nodes: set[int],
    alpha: float = 0.85,
    iterations: int = 30,
    tolerance: float = 1e-4,
    min_confidence: float = 0.5,
) -> PPRResult:
    """Run Personalized PageRank from the given seeds.

    Args:
        graph_db: Filesystem path to graph.db. The function opens its own
            connection and closes it before returning.
        seed_nodes: Node ids to concentrate the restart vector on. If
            empty, returns an empty result without opening the DB —
            Module 5 will abstain.
        alpha: Damping factor (probability of *following* an edge rather
            than restarting). 0.85 matches standard PageRank.
        iterations: Hard cap on power-iteration steps.
        tolerance: l-infinity convergence threshold. Iteration halts when
            the max per-node delta drops below this value.
        min_confidence: Edges with confidence below this are excluded.

    Returns:
        PPRResult. ``node_scores`` is empty when ``seed_nodes`` is empty
        (NOT a uniform distribution).
    """
    if not seed_nodes:
        return PPRResult(node_scores={}, iterations_run=0, converged=True)

    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return PPRResult(node_scores={}, iterations_run=0, converged=False)

    try:
        adj, out_deg = _build_adjacency(conn, min_confidence)
    finally:
        conn.close()

    # Restart vector: 1.0 mass spread uniformly over seeds, 0 elsewhere.
    n_seeds = len(seed_nodes)
    restart: dict[int, float] = {nid: 1.0 / n_seeds for nid in seed_nodes}

    # Initialize scores at the restart distribution.
    scores: dict[int, float] = dict(restart)

    converged = False
    iters_run = 0
    for step in range(iterations):
        iters_run = step + 1
        new_scores: dict[int, float] = defaultdict(float)

        # Random-walk component: alpha * (P @ scores). For each node with
        # mass, push mass uniformly across its out-neighbors.
        for src, mass in scores.items():
            if mass == 0.0:
                continue
            neighbors = adj.get(src)
            if not neighbors:
                # Dangling node — push back into the restart vector so
                # mass is preserved (standard handling).
                for sid, share in restart.items():
                    new_scores[sid] += alpha * mass * share
                continue
            n_out = out_deg[src]
            push = alpha * mass / n_out
            for dst in neighbors:
                new_scores[dst] += push

        # Restart component: (1 - alpha) * restart vector.
        for sid, share in restart.items():
            new_scores[sid] += (1.0 - alpha) * share

        # Convergence check: l-infinity over union of keys.
        max_delta = 0.0
        all_keys = set(new_scores) | set(scores)
        for key in all_keys:
            delta = abs(new_scores.get(key, 0.0) - scores.get(key, 0.0))
            if delta > max_delta:
                max_delta = delta

        scores = dict(new_scores)
        if max_delta < tolerance:
            converged = True
            break

    # Drop near-zero entries to keep telemetry clean.
    pruned = {nid: s for nid, s in scores.items() if s > 1e-9}

    return PPRResult(
        node_scores=pruned,
        iterations_run=iters_run,
        converged=converged,
    )


def aggregate_scores_by_file(
    node_scores: dict[int, float],
    graph_db: str,
) -> dict[str, tuple[float, int]]:
    """Sum node scores by their file_path.

    Returns:
        ``{file_path: (sum_score, node_count)}``.
    """
    if not node_scores:
        return {}
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return {}
    try:
        ids = list(node_scores.keys())
        # Chunk the IN clause to avoid SQLite's parameter limit.
        out: dict[str, tuple[float, int]] = {}
        chunk = 500
        for i in range(0, len(ids), chunk):
            slab = ids[i : i + chunk]
            placeholders = ",".join("?" for _ in slab)
            cursor = conn.execute(
                f"SELECT id, file_path FROM nodes WHERE id IN ({placeholders})",
                tuple(slab),
            )
            for nid, fpath in cursor.fetchall():
                if not fpath:
                    continue
                score = node_scores.get(nid, 0.0)
                prev_sum, prev_count = out.get(fpath, (0.0, 0))
                out[fpath] = (prev_sum + score, prev_count + 1)
        return out
    finally:
        conn.close()
