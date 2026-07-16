from __future__ import annotations

import json
import sqlite3

import artifact_deepswe.gt_mini_patch as gmp
from groundtruth.runtime.gateway import (
    GatewayState, SearchScopeRelation, SearchScopeSelection,
    _certified_search_scope,
)
from groundtruth.runtime.native_render import render_related_files_native


def _graph(path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, file_path TEXT, is_test INTEGER DEFAULT 0);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
          type TEXT, resolution_method TEXT, confidence REAL);
        CREATE TABLE project_meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO project_meta VALUES('post_revision', 'graph-rev-7');
        INSERT INTO nodes VALUES(1, 'src/anchor.py', 0);
        INSERT INTO nodes VALUES(2, 'src/certified.py', 0);
        INSERT INTO nodes VALUES(3, 'src/guess.py', 0);
        INSERT INTO nodes VALUES(4, 'src/low.py', 0);
        INSERT INTO nodes VALUES(5, 'tests/test_anchor.py', 1);
        INSERT INTO nodes VALUES(6, 'src/incoming.py', 0);
        INSERT INTO edges VALUES(10, 1, 2, 'CALLS', 'import', 1.0);
        INSERT INTO edges VALUES(11, 1, 3, 'CALLS', 'name_match', 1.0);
        INSERT INTO edges VALUES(12, 1, 4, 'CALLS', 'type_flow', 0.89);
        INSERT INTO edges VALUES(13, 1, 5, 'CALLS', 'import', 1.0);
        INSERT INTO edges VALUES(14, 6, 1, 'CALLS', 'type_flow', 0.95);
        """
    )
    con.commit()
    con.close()


def test_scope_selection_is_certified_revision_bound_and_hedged(tmp_path):
    db = tmp_path / "graph.db"
    _graph(db)
    state = GatewayState(graph_db=str(db), repo_root=str(tmp_path), issue_text="x")

    selection = _certified_search_scope(state, "src/anchor.py")

    assert selection.graph_revision == "graph-rev-7"
    assert [(r.related_file, r.resolution_method, r.confidence) for r in selection.relations] == [
        ("src/certified.py", "import", 1.0),
        ("src/incoming.py", "type_flow", 0.95),
    ]
    rendered = render_related_files_native(selection.relations)
    assert rendered.splitlines() == [
        "src/certified.py: note: related file to inspect (certified import relation)",
        "src/incoming.py: note: related file to inspect (certified type_flow relation)",
    ]
    assert "must" not in rendered.lower()


def test_missing_revision_is_correct_or_quiet(tmp_path):
    db = tmp_path / "graph.db"
    _graph(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM project_meta")
    con.commit()
    con.close()
    state = GatewayState(graph_db=str(db), repo_root=str(tmp_path), issue_text="x")
    assert not _certified_search_scope(state, "src/anchor.py").relations
    assert not _certified_search_scope(state, "tests/test_anchor.py").relations


def test_scope_flag_off_is_exactly_byte_identical(monkeypatch):
    monkeypatch.delenv("GT_SCOPE_NATIVE", raising=False)
    base = "src/a.py:1:run"
    selection = SearchScopeSelection("rev", (
        SearchScopeRelation("src/b.py", "import", 1.0, 10),
    ))
    assert gmp._splice_search_scope(
        base, selection, render_related_files_native) == (base, None)


def test_terminal_scope_control_uses_final_localization_identity_and_revision(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(gmp, "_action_count", 7)
    gmp._terminal_lane_controls.clear()
    text = "src/a.py:1:run\nsrc/b.py: note: related file to inspect (certified import relation)"
    decision = gmp._post_search_decision(
        text, "ranked_localization", "localization",
        scope_graph_revision="graph-rev-7",
        scope_relations=(("src/b.py", "import", 1.0, 10),),
    )
    lineage = decision.lineage()
    extra = gmp._lane_delivery_extra(
        "post_search.localize", text, "src/a.py", "post_search",
        lineage=lineage, identity=decision,
    )
    gmp._stage_terminal_lane_control(
        "post_search.localize", text, feature_id="GT_SCOPE_NATIVE",
        decision_site="mini_seam.scope.native_render", decision="APPLIED",
        reason="certified_search_scope_shaped_localization",
    )
    gmp._record_terminal_lane_controls(
        "post_search.localize", text, "\n" + text, "src/a.py",
        delivery_extra=extra,
    )

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    row = next(r for r in rows if r.get("control_ref", {}).get("feature_id") == "GT_SCOPE_NATIVE")
    assert row["fact_class"] == "localization"
    assert row["candidate_id"] == extra["candidate_id"]
    assert extra["graph_revision"] == "graph-rev-7"
    assert extra["scope_relations"] == [{
        "edge_id": 10, "related_file": "src/b.py",
        "resolution_method": "import", "confidence": 1.0,
    }]


def test_scope_control_loser_and_non_scope_candidate_receive_no_credit(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(gmp, "_action_count", 8)
    gmp._terminal_lane_controls.clear()
    text = "src/a.py:1:run\nsrc/b.py: note: related file to inspect (certified import relation)"
    gmp._stage_terminal_lane_control(
        "post_search.localize", text, feature_id="GT_SCOPE_NATIVE",
        decision_site="mini_seam.scope.native_render", decision="APPLIED",
        reason="certified_search_scope_shaped_localization",
    )
    gmp._discard_terminal_lane_controls([8])
    lineage = gmp._post_search_decision(
        text, "ranked_localization", "localization").lineage()
    extra = gmp._lane_delivery_extra(
        "post_search.localize", text, "src/a.py", "post_search", lineage=lineage)
    gmp._record_terminal_lane_controls(
        "post_search.localize", text, "\n" + text, "src/a.py", delivery_extra=extra)
    assert not ledger.exists()
    assert "graph_revision" not in extra
