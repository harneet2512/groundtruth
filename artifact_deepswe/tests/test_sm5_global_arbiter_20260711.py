"""SM-5 — the ONE global ranked competition wired into the mini seam.

Proves, on the REAL seam (gt_mini_patch._augment_output / _global_pool_flush):
  * GT_GLOBAL_ARBITER OFF (default) is BYTE-IDENTICAL to the pre-SM-5 three planes;
  * GT_GLOBAL_ARBITER ON, single-plane turn: the ONE gateway fact still delivers,
    byte-identical to the inline path (the thunk == the inline delivery);
  * the flush delivers AT MOST ONE global dose across planes (the <=1 GLOBAL dose),
    the highest ladder class winning;
  * the UNIFIED dedup — a candidate whose dedup_key is already delivered loses;
  * every LOSER is logged with its reason (the generalized loser log);
  * the winner's unified dedup_key is stamped for cross-plane, cross-turn dedup;
  * two BITING MUTATIONS (deliver all thunks / skip the winner-key stamp) each redden.

Hermetic: a synthetic graph.db, no ONNX / no repo checkout, GT_POST_SEARCH off so on a
search turn the ONLY plane in play is the gateway (a clean single-plane byte check).
"""
from __future__ import annotations

import sqlite3

import pytest

import gt_mini_patch as g
from groundtruth.runtime import global_arbiter as ga


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


@pytest.fixture
def seam(tmp_path, monkeypatch):
    """A 2-file 'run' graph (AMBIGUOUS_HIT -> def_ref_partition) wired into the seam;
    GT_POST_SEARCH off so only the gateway can add a delta on a search turn."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    monkeypatch.delenv("GT_GLOBAL_ARBITER", raising=False)
    g._reset_oracle_state()
    yield db
    g._reset_oracle_state()


def _run(cmd, output, *, rc=0):
    action, out = {"command": cmd}, {"output": output, "returncode": rc}
    g._augment_output(action, out)
    return out["output"]


# --------------------------------------------------------------------------- #
# flag reader
# --------------------------------------------------------------------------- #
def test_flag_reader_default_off(monkeypatch):
    monkeypatch.delenv("GT_GLOBAL_ARBITER", raising=False)
    assert g._global_arbiter_on() is False
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    assert g._global_arbiter_on() is True
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")
    assert g._global_arbiter_on() is False


# --------------------------------------------------------------------------- #
# byte-identical OFF + single-plane parity ON (the thunk == the inline delivery)
# --------------------------------------------------------------------------- #
def test_off_is_byte_identical_and_on_single_plane_matches(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")

    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")
    off = _run("grep -rn run .", grep_out)
    g._reset_oracle_state()

    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    on = _run("grep -rn run .", grep_out)

    assert on.startswith(grep_out)              # TITO law 1/2: pure suffix
    assert "a/x.py:10" in on[len(grep_out):]    # the gateway fact still reached the model
    assert on == off                            # single-plane: the pool thunk == inline path


def test_off_gateway_dark_is_untouched(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "0")       # gateway dark
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    out = _run("grep -rn run .", grep_out)
    assert out == grep_out                       # arbiter with an empty pool == silence


# --------------------------------------------------------------------------- #
# the flush — <=1 global dose, unified dedup, loser log, winner stamp
# --------------------------------------------------------------------------- #
def _thunk_pool(out):
    delivered = []

    def mk(cand, tag):
        def _t():
            out["output"] += f"\n<{tag}>"
            delivered.append(tag)
        return (cand, _t)

    return delivered, mk


def test_flush_delivers_at_most_one_highest_class(seam):
    g._reset_oracle_state()
    out = {"output": "BASE"}
    delivered, mk = _thunk_pool(out)
    pool = [
        mk(ga.Candidate(plane=ga.PLANE_LANE_A, kind="l3.contract", dedup_key="lo", seq=0), "LO"),
        mk(ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict", dedup_key="hi", seq=1), "HI"),
    ]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    # MUTATION[flush delivers every thunk] -> delivered == ["LO","HI"], this REDDENS.
    assert delivered == ["HI"]                   # exactly ONE dose, highest ladder class
    assert out["output"].count("<") == 1


def test_flush_stamps_winner_unified_key(seam):
    g._reset_oracle_state()
    out = {"output": "B"}
    delivered, mk = _thunk_pool(out)
    pool = [mk(ga.Candidate(plane=ga.PLANE_GATEWAY, kind="caller_break",
                            dedup_key="WKEY", seq=0), "W")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    # MUTATION[skip winner-key stamp] -> "WKEY" absent, this REDDENS.
    assert "WKEY" in g._EPISODE.delivered_dedup   # next turn dedups it cross-plane


def test_flush_unified_dedup_suppresses_already_delivered(seam):
    g._reset_oracle_state()
    g._EPISODE.delivered_dedup.add("DUP")
    out = {"output": "B"}
    delivered, mk = _thunk_pool(out)
    pool = [mk(ga.Candidate(plane=ga.PLANE_GATEWAY, kind="caller_break",
                            dedup_key="DUP", seq=0), "D")]
    g._global_pool_flush(pool, kkind="post_edit", kf="", krel="")
    assert delivered == []                        # already-delivered key -> no re-dose
    assert out["output"] == "B"


def test_flush_logs_every_loser(seam, monkeypatch):
    g._reset_oracle_state()
    suppressed = []
    monkeypatch.setattr(g, "_record_hook_suppress",
                        lambda kind, reason="": suppressed.append((kind, reason)))
    out = {"output": "B"}
    delivered, mk = _thunk_pool(out)
    pool = [
        mk(ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict", dedup_key="a", seq=0), "A"),
        mk(ga.Candidate(plane=ga.PLANE_STEER, kind="l5.stuck", dedup_key="b", seq=1), "B"),
        mk(ga.Candidate(plane=ga.PLANE_LANE_A, kind="internal_prior_xyz", dedup_key="c", seq=2), "C"),
    ]
    g._global_pool_flush(pool, kkind="post_edit", kf="", krel="")
    assert delivered == ["A"]                      # <=1 dose
    kinds = {k for k, _r in suppressed}
    assert "l5.stuck" in kinds                     # the outranked steer is logged
    assert "internal_prior_xyz" in kinds           # the off-ladder internal is logged


def test_flush_empty_pool_is_silence(seam):
    g._reset_oracle_state()
    out = {"output": "B"}
    g._global_pool_flush([], kkind="post_edit", kf="", krel="")
    assert out["output"] == "B"


# --------------------------------------------------------------------------- #
# leak=0 — a delivered winner passes the seam's own leak firewall
# --------------------------------------------------------------------------- #
def test_delivered_winner_is_leak_clean(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    out = _run("grep -rn run .", grep_out)
    delta = out[len(grep_out):]
    from groundtruth.runtime.native_render import contains_test_identity
    assert not contains_test_identity(delta)       # leak=0: no test identity delivered
