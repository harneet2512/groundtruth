"""Measurement-integrity pins for scripts/swebench/gt_deep_metrics.py (G3, G14).

G3: import/same_file (tree-sitter deterministic) edges were mislabelled as
'LSP-enriched' (113x inflation, 1934 vs 17 real lsp edges). The graph census must
name them honestly and expose the REAL lsp-stamped count separately.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "swebench" / "gt_deep_metrics.py"
_spec = importlib.util.spec_from_file_location("gt_deep_metrics", _MOD)
assert _spec and _spec.loader
dm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dm)


def _mk_graph(path: Path) -> str:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, return_type TEXT)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, resolution_method TEXT, confidence REAL)"
    )
    con.executemany(
        "INSERT INTO nodes (id, name, return_type) VALUES (?,?,?)",
        [(1, "a", "User"), (2, "b", None), (3, "c", "")],
    )
    # deterministic edges: import + same_file; NO lsp edges at all
    con.executemany(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (?,?,?,?,?)",
        [
            (1, 2, "CALLS", "import", 1.0),
            (2, 3, "CALLS", "same_file", 1.0),
            (1, 3, "CALLS", "name_match", 0.2),
        ],
    )
    con.commit()
    con.close()
    return str(path)


def test_g3_deterministic_edges_not_labelled_lsp(tmp_path: Path) -> None:
    db = _mk_graph(tmp_path / "graph.db")
    out = dm._from_graph_db(db)
    # honest names present
    assert out["deterministic_edge_count"] == 2  # import + same_file
    assert out["return_type_signature_count"] == 1  # only node 'a'
    # the REAL lsp count is zero on a graph with no lsp-stamped edges
    assert out["lsp_stamped_edge_count"] == 0
    # the mislabelled fields are GONE
    assert "lsp_enriched_edge_count" not in out
    assert "lsp_return_type_signature_count" not in out


def test_g3_lsp_stamped_counts_only_lsp_edges(tmp_path: Path) -> None:
    db = str(tmp_path / "g2.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, return_type TEXT)")
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, resolution_method TEXT, confidence REAL)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'a',NULL)")
    con.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) VALUES (?,?,?,?,?)",
        [(1, 1, "CALLS", "lsp", 1.0), (1, 1, "CALLS", "lsp_verified", 1.0),
         (1, 1, "CALLS", "import", 1.0)],
    )
    con.commit()
    con.close()
    out = dm._from_graph_db(db)
    assert out["lsp_stamped_edge_count"] == 2  # lsp + lsp_verified only
    assert out["deterministic_edge_count"] == 1  # import


def test_g14_d8_missing_is_null() -> None:
    assert dm.d8(None) is None
    assert dm.d8(float("nan")) is None
    assert dm.d8(1 / 3) == round(1 / 3, 8)


def test_g14_pair_delta_null_when_arm_missing() -> None:
    """A delta over a missing arm metric is unknown -> None, never fabricated 0.0."""
    gt = {"agent": {"action_count": None}}
    base = {"agent": {"action_count": 10}}
    delta = dm.pair(gt, base)
    assert delta["action_count_delta"] is None
