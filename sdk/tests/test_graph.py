from __future__ import annotations

from pathlib import Path

from groundtruth.graph import GraphStore


def test_graph_store_callers_and_callees(graph_db_path: Path) -> None:
    store = GraphStore(str(graph_db_path), read_only=False)
    try:
        mid = store.find_symbol("mid")
        assert mid
        mid_id = int(mid[0]["id"])

        callers = store.callers_of(mid_id)
        assert {int(c["source_id"]) for c in callers} == {1, 3}

        callees = store.callees_of(mid_id)
        assert {int(c["target_id"]) for c in callees} == {3}
    finally:
        store.close()


def test_symbols_in_file(graph_db_path: Path) -> None:
    store = GraphStore(str(graph_db_path), read_only=False)
    try:
        rows = store.symbols_in_file("src/b.py")
        assert [r["name"] for r in rows] == ["mid"]
    finally:
        store.close()


def test_ego_directions(graph_db_path: Path) -> None:
    store = GraphStore(str(graph_db_path), read_only=False)
    try:
        callers_only = store.ego("leaf", depth=3, direction="callers", deterministic_only=False)
        assert "pkg.leaf" in callers_only
        assert "pkg.mid" in callers_only
        assert "pkg.top" in callers_only

        callees_only = store.ego("top", depth=3, direction="callees", deterministic_only=False)
        assert "pkg.top" in callees_only
        assert "pkg.mid" in callees_only
        assert "pkg.leaf" in callees_only
    finally:
        store.close()
