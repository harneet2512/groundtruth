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

# I2 (depth never enters reach/RANK): this proximity count feeds `anchor_prox` -> the
# W_PROX rank term in v7_4_brief._total_score (W_PROX 0.05, boosted to 0.12 in the
# function-level regime). Reuse the D5 single-source predicate (the same one G01 applied
# to graph_reach) so promoted DEPTH edges (READS/WRITES/RAISES/CO_SERIALIZES/DATA_FLOW +
# any promote_% provenance, all minted at conf 1.0) can't pass the >=0.7 gate, inflate the
# anchor neighbor set, and shift rank. graph_localizer does NOT import this module (no cycle).
from groundtruth.pretask.graph_localizer import _degree_edge_filter, _normalize as _norm_path

# BUG-5 (2026-06-15): the 1-hop proximity SELECT gated edges at confidence >= 0.7,
# which BLANKED every name_match (0.6) and NULL-confidence neighbor on the
# name-match-heavy graphs that are 70-80% of real repos — W_PROX went dark exactly
# where the agent needs the structural signal. Lower the floor to the localizer's
# own name_match admission floor (0.5) for consistency. The categorical degree
# filter (promoted-DEPTH exclusion) still applies, so this admits real name_match
# CALLS/IMPORTS neighbors, not promoted depth.
_NAME_MATCH_FLOOR = 0.5


def compute_anchor_proximity(
    trusted_anchors: list[str],
    graph_db: str,
) -> dict[str, float]:
    """Return {file_path: anchor_prox_score} for all 1-hop neighbors of trusted anchors."""
    if not trusted_anchors or not graph_db:
        return {}

    conn = sqlite3.connect(graph_db)
    c = conn.cursor()

    # Count distinct trusted anchors that can reach each file in ≤1 hop.
    # BUG-1: keys are canonicalized so a normalized anchor matches a RAW DB path
    # (Windows ``a\b.py`` / ``./a/b.py``). The anchor seed set is normalized; the
    # edge endpoints are normalized below before comparison.
    _anchor_set = {_norm_path(a) for a in trusted_anchors}
    neighbor_count: dict[str, set[str]] = defaultdict(set)

    # Self: each anchor is reachable from itself (0 hops)
    for anchor in _anchor_set:
        neighbor_count[anchor].add(anchor)

    # 1-hop neighbors — fetch all qualifying cross-file edges, normalize, match the
    # source against the normalized anchor set in Python (SQLite cannot normalize
    # the path in-clause; matching raw against normalized anchors silently missed).
    c.execute(
        f"""
        SELECT DISTINCT n1.file_path AS src_file, n2.file_path AS dst_file
        FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE n1.file_path IS NOT NULL
          AND n2.file_path IS NOT NULL
          AND n1.file_path != n2.file_path
          AND COALESCE(e.confidence, 0.5) >= {_NAME_MATCH_FLOOR}
          AND {_degree_edge_filter('e')}
        """
    )
    for src, dst in c.fetchall():
        nsrc, ndst = _norm_path(src), _norm_path(dst)
        if nsrc in _anchor_set and nsrc != ndst:
            neighbor_count[ndst].add(nsrc)

    conn.close()

    return {
        fp: min(1.0, len(anchors) / 3.0)
        for fp, anchors in neighbor_count.items()
    }
