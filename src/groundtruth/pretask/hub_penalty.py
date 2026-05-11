"""Soft hub penalty for v7.4 brief.

Hub files (high in-degree) are sometimes legitimate fix sites (cross-cutting
bugs), so we apply only a soft tanh-bounded penalty as a tie-break,
never as a hard veto.

W_HUB is capped at 0.10 to ensure the penalty never dominates.
"""
from __future__ import annotations

import math
import sqlite3
HUB_SCALE = 50.0  # in-degree at which tanh reaches ~0.76; tuneable
W_HUB_MAX = 0.10  # hard cap on hub penalty weight


def compute_hub_penalties(graph_db: str) -> dict[str, float]:
    """Return {file_path: hub_penalty} where penalty = tanh(in_degree / HUB_SCALE).

    Result is in [0, 1). Caller multiplies by W_HUB (≤ W_HUB_MAX) before use.
    """
    if not graph_db:
        return {}

    conn = sqlite3.connect(graph_db)
    c = conn.cursor()
    # Count incoming CALLS edges only per file (via target node's file_path).
    # EXTENDS/IMPLEMENTS edges indicate architectural hierarchy and should not
    # contribute to hub penalty — a base class is not a "hub" just because many
    # classes inherit from it.
    c.execute(
        """
        SELECT n.file_path, COUNT(*) as in_degree
        FROM edges e
        JOIN nodes n ON e.target_id = n.id
        WHERE n.file_path IS NOT NULL
          AND e.type = 'CALLS'
        GROUP BY n.file_path
        """
    )
    rows = c.fetchall()
    conn.close()

    return {
        fp: math.tanh(float(in_deg) / HUB_SCALE)
        for fp, in_deg in rows
    }
