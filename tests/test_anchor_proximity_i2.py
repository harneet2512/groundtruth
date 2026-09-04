"""I2 (depth↔rank isolation) regression for anchor_proximity — the W_PROX leak.

The adversarial max-LIPI found compute_anchor_proximity's 1-hop SELECT gated only on
`confidence >= 0.7` with NO type/provenance exclusion, so promoted DEPTH edges (conf 1.0,
cross-file) inflated the anchor neighbor set -> `anchor_prox` -> the W_PROX rank term in
v7_4_brief._total_score. G01 fixed the reach term but stopped one term short of prox, and
the reach I2 suite did not cover proximity — which is exactly why the leak slipped. This
pins it.

THE INVARIANT: compute_anchor_proximity returns BYTE-IDENTICAL scores with vs without
promoted READS/WRITES/RAISES/CO_SERIALIZES/DATA_FLOW edges layered on. Promoted depth is
proximity-invisible (hence rank-invisible). Deterministic unit test — no agent/task run.
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.anchor_proximity import compute_anchor_proximity
from groundtruth.pretask.graph_localizer import _PROMOTED_EDGE_TYPES


def _build_db(path, *, promoted: bool) -> None:
    con = sqlite3.connect(str(path))
    c = con.cursor()
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT)")
    c.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INT, "
        "target_id INT, type TEXT, confidence REAL, resolution_method TEXT)"
    )
    for nid, fp in [(1, "a.py"), (2, "b.py"), (3, "c.py"), (4, "d.py")]:
        c.execute("INSERT INTO nodes (id, file_path) VALUES (?, ?)", (nid, fp))
    # legit cross-file CALLS edge a->b at conf 0.9 — MUST be counted as a neighbor
    c.execute(
        "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
        "VALUES (1, 2, 'CALLS', 0.9, 'import')"
    )
    if promoted:
        # promoted DEPTH edges a->c, a->d at conf 1.0 — MUST NOT inflate the neighbor set
        c.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
            "VALUES (1, 3, 'READS', 1.0, 'promote_field_read')"
        )
        c.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
            "VALUES (1, 4, 'CO_SERIALIZES', 1.0, 'promote_serde')"
        )
    con.commit()
    con.close()


def test_proximity_byte_identical_with_vs_without_promoted(tmp_path):
    anchors = ["a.py"]
    wo = tmp_path / "wo.db"
    w = tmp_path / "w.db"
    _build_db(wo, promoted=False)
    _build_db(w, promoted=True)
    s_wo = compute_anchor_proximity(anchors, str(wo))
    s_w = compute_anchor_proximity(anchors, str(w))
    assert s_wo == s_w, f"W_PROX leak: promoted edges shifted proximity {s_wo} -> {s_w}"
    # and the legit CALLS neighbor IS still counted (the filter must not over-suppress)
    assert "b.py" in s_w


def test_proximity_excludes_each_promoted_type(tmp_path):
    anchors = ["a.py"]
    wo = tmp_path / "wo.db"
    _build_db(wo, promoted=False)
    base = compute_anchor_proximity(anchors, str(wo))
    for et in _PROMOTED_EDGE_TYPES:
        path = tmp_path / f"{et}.db"
        con = sqlite3.connect(str(path))
        c = con.cursor()
        c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT)")
        c.execute(
            "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INT, "
            "target_id INT, type TEXT, confidence REAL, resolution_method TEXT)"
        )
        for nid, fp in [(1, "a.py"), (2, "b.py"), (3, "c.py")]:
            c.execute("INSERT INTO nodes (id, file_path) VALUES (?, ?)", (nid, fp))
        c.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
            "VALUES (1, 2, 'CALLS', 0.9, 'import')"
        )
        c.execute(
            "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
            "VALUES (1, 3, ?, 1.0, 'promote_x')",
            (et,),
        )
        con.commit()
        con.close()
        assert compute_anchor_proximity(anchors, str(path)) == base, (
            f"{et} leaked into anchor proximity -> W_PROX rank"
        )
