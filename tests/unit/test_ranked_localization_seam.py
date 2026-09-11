import sqlite3
from types import SimpleNamespace

import pytest

import groundtruth.runtime.gateway as gateway
from groundtruth.runtime.gateway import (
    GatewayState,
    ToolEvent,
    produce_raw,
)


def _mk_graph(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL, metadata TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO nodes VALUES (1,'Function','snapshot','pkg.snap.snapshot',"
        "'pkg/snap.py',5,20,'def snapshot()','',1,0,'python',NULL)"
    )
    con.commit()
    con.close()


def test_localizer_seam_is_wired():
    # The comparison-control kill switch left _localize=None in product runs,
    # so ranked_localization abstained 100% of the time while the harness
    # provisioned the index. The seam must resolve to graph_localizer.localize.
    assert gateway._localize is not None
    assert callable(gateway._localize)


def test_ranked_localization_emits_rows_when_localizer_has_candidates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "snap.py").write_text(
        "def snapshot():\n    return 1\n", encoding="utf-8"
    )
    db = tmp_path / "graph.db"
    _mk_graph(str(db))

    fake = SimpleNamespace(
        candidates=[
            SimpleNamespace(file_path="pkg/snap.py", score=0.91),
        ],
        anchor_symbols=["snapshot"],
    )
    monkeypatch.setattr(gateway, "_localize", lambda *a, **k: fake)

    state = GatewayState(
        repo_root=str(tmp_path),
        graph_db=str(db),
        issue_text="add snapshot support",
    )
    event = ToolEvent(
        kind="search",
        command="grep -rn snapshot pkg/",
        semantic_events=("search_result",),
        semantics_authoritative=True,
    )
    candidates = produce_raw(event, state)
    views = [c for c in candidates if c.evidence_type == "localization"]
    assert views, "ranked_localization should emit a localization envelope"
    rows = views[0].native_args.get("rows") or ()
    assert ("pkg/snap.py", 5, "snapshot") in rows
