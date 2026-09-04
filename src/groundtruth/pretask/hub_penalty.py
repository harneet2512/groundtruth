"""Soft hub penalty for v7.4 brief.

Hub files (high in-degree) are sometimes legitimate fix sites (cross-cutting
bugs), so we apply only a soft tanh-bounded penalty as a tie-break,
never as a hard veto.

W_HUB is capped at 0.10 to ensure the penalty never dominates.
"""

from __future__ import annotations

import math
import sqlite3

from groundtruth.pretask.graph_localizer import _normalize as _norm_path

HUB_SCALE = 50.0  # in-degree at which tanh reaches ~0.76; tuneable
W_HUB_MAX = 0.10  # hard cap on hub penalty weight
# BUG-5 (2026-06-15): the in-degree count gated on confidence >= 0.7, so a 0.6
# name_match hub (the dominant hub shape on name-match-heavy graphs) accrued ZERO
# penalty and ESCAPED the demotion — the exact false positive the hub penalty
# exists to bite (a high-in-degree hub that matched issue keywords). Count hub
# in-degree at the 0.5 name_match floor so a 0.6 name_match hub is penalized.
_NAME_MATCH_FLOOR = 0.5


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
        f"""
        SELECT n.file_path, COUNT(*) as in_degree
        FROM edges e
        JOIN nodes n ON e.target_id = n.id
        WHERE n.file_path IS NOT NULL
          AND e.type = 'CALLS'
          AND COALESCE(e.confidence, 0.5) >= {_NAME_MATCH_FLOOR}
        GROUP BY n.file_path
        """
    )
    rows = c.fetchall()
    conn.close()

    # BUG-1: re-key by canonical path so a normalized candidate's hub_pen lookup
    # hits a RAW DB spelling; sum the in-degree when two spellings collapse.
    out: dict[str, float] = {}
    _indeg: dict[str, int] = {}
    for fp, in_deg in rows:
        _indeg[_norm_path(fp)] = _indeg.get(_norm_path(fp), 0) + int(in_deg)
    for fp, in_deg in _indeg.items():
        out[fp] = math.tanh(float(in_deg) / HUB_SCALE)
    return out
