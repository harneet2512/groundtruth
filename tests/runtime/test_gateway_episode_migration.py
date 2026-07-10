"""Step-1 strangler migration: GatewayState is a thin facade over EpisodeState.

The three fragmented Gateway stores (the probe-stem ``ledger`` dict, the
``delivered_keys`` dedup set, the ``edit_events`` list) now have EXACTLY ONE owner —
the embedded :class:`EpisodeState`. GatewayState exposes them as delegating
properties so every producer's ``state.ledger[...] = ...`` / ``state.delivered_keys.add``
/ ``state.edit_events.append`` is byte-identical, while the storage lives in the
episode. These tests pin the single-owner invariant (and the M1 mutation bites if a
second dedup owner is ever reintroduced).
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.gateway import GatewayState, ToolEvent, augment


@pytest.fixture(autouse=True)
def _gateway_on(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    yield


def _mk_graph(tmp_path, nodes):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5), n.get("is_test", 0),
             n.get("language", "python")))
    con.commit()
    con.close()
    return db


# ---- M1 MUTATION target: single dedup owner (identity, not just equality) ----
def test_m1_delivered_keys_is_the_episode_chain(tmp_path):
    """MUTATION M1 bites here: ``delivered_keys`` MUST be the very same object as the
    episode's ``delivered_dedup`` — one owner. Reintroducing a second GatewayState-
    local set breaks this identity."""
    st = GatewayState(graph_db="x", repo_root=str(tmp_path))
    assert st.delivered_keys is st.episode.delivered_dedup
    assert st.ledger is st.episode.probe_ledger
    assert st.edit_events is st.episode.edit_events


def test_gateway_state_accepts_an_explicit_episode(tmp_path):
    """The facade may be constructed over a caller-supplied EpisodeState (the W2 seam
    passes the one live episode); absent one, it creates a fresh episode."""
    ep = EpisodeState(episode_id="task-1")
    st = GatewayState(graph_db="x", repo_root=str(tmp_path), episode=ep)
    assert st.episode is ep
    assert st.delivered_keys is ep.delivered_dedup
    fresh = GatewayState(graph_db="x", repo_root=str(tmp_path))
    assert isinstance(fresh.episode, EpisodeState) and fresh.episode is not ep


def test_delivery_flows_through_the_episode_chain(tmp_path):
    """A DELIVERED fact lands its dedup key in the EPISODE's chain (the one owner),
    and the ledger probe history is recorded in the episode's probe_ledger.
    ADJUSTED (stamp-at-seal, bounce 2026-07-10): augment PRODUCES and reads; the
    seal step (the delivery commit) stamps the chain — exercised via the adapter's
    seal_delivery(dedup_chain=...), which writes THROUGH the facade."""
    from groundtruth.runtime.adapters.miniswe import seal_delivery

    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    st = GatewayState(graph_db=db, repo_root=str(tmp_path))
    ev = ToolEvent("search", "grep -rn run .", "a/x.py:10: run\nb/y.py:20: run")
    out = augment(ev, st)
    assert out                                   # a partition produced
    assert st.episode.delivered_dedup == set()   # production never stamps (F1)
    seal_delivery(out[0], episode_id="t", event_id="1", parent_hash="",
                  rendered_bytes=b"delta", renderer_id="native",
                  dedup_chain=st.delivered_keys)
    assert st.episode.delivered_dedup            # its key lives in the episode chain
    assert st.episode.delivered_dedup == st.delivered_keys
    assert "run" in st.episode.probe_ledger      # probe recorded in the episode ledger
    # a DELIVERED fact is latched by the SAME episode chain
    assert augment(ev, st) == []


def test_reset_attempt_reclears_gateway_stores(tmp_path):
    """Because the stores ARE the episode, reset_attempt() clears them through the
    facade — an in-process retry starts with an empty ledger / dedup chain.
    ADJUSTED (stamp-at-seal, bounce 2026-07-10): the dedup key is stamped via the
    delivery commit (seal), then reset clears it through the facade."""
    from groundtruth.runtime.adapters.miniswe import seal_delivery

    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    st = GatewayState(graph_db=db, repo_root=str(tmp_path))
    out = augment(ToolEvent("search", "grep -rn run .", "a/x.py:10: run\nb/y.py:20: run"), st)
    assert out
    seal_delivery(out[0], episode_id="t", event_id="1", parent_hash="",
                  rendered_bytes=b"delta", renderer_id="native",
                  dedup_chain=st.delivered_keys)
    assert st.delivered_keys and st.ledger
    st.episode.reset_attempt()
    assert st.delivered_keys == set() and st.ledger == {} and st.edit_events == []
