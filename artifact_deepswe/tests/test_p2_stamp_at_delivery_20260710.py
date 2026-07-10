"""P2 STAMP-AT-DELIVERY — D-4 (lattice latch), D-5 (Lane-B winner hash), S-2 (band).

Each stamp/latch is consumed only on a REAL delivered outcome, never at production —
so a producer suppressed downstream (Lane-A dedup / gate-to-append crash / empty
render) never leaves a phantom 'delivered'/'answered'/'spent' state.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "artifact_deepswe"), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gt_mini_patch as g  # noqa: E402


def _mk_graph(tmp: Path) -> str:
    db = str(tmp / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY,label TEXT,name TEXT,"
        "qualified_name TEXT,file_path TEXT,start_line INTEGER,end_line INTEGER,"
        "signature TEXT,return_type TEXT,is_exported INTEGER,is_test INTEGER,"
        "language TEXT,parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY,source_id INTEGER,target_id INTEGER,"
        "type TEXT,source_line INTEGER,source_file TEXT,resolution_method TEXT,"
        "confidence REAL,metadata TEXT);")
    con.executemany(
        "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "Function", "get_user", "a.x.get_user", "a/x.py", 1, 2,
          "def get_user(uid)", None, 1, 0, "python", None),
         (2, "Function", "handler", "b.y.handler", "b/y.py", 3, 4,
          "def handler(uid)", None, 1, 0, "python", None)])
    con.executemany(
        "INSERT INTO edges(source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES(?,?,?,?,?,?,?)",
        [(2, 1, "CALLS", 4, "b/y.py", "import", 1.0)])
    con.commit()
    con.close()
    return db


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path / "certs"))
    (tmp_path / "certs").mkdir(exist_ok=True)
    for k in ("GT_BASELINE", "GT_POST_SEARCH_NATIVE"):
        monkeypatch.delenv(k, raising=False)
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._GT_BASELINE = False
    g._POST_SEARCH_ON = True
    g._reset_oracle_state()
    return g, tmp_path


# ── D-4: lattice latch stamped at DELIVERY (through _augment_output) ─────────
def test_d4_latch_stamped_only_at_delivery(wired):
    m, _tmp = wired
    cmd = "grep -rn get_user ."
    out = {"output": "a/x.py:1:def get_user(uid):", "returncode": 0}
    m._augment_output({"command": cmd}, out)
    delivered = out["output"][len("a/x.py:1:def get_user(uid):"):]
    assert "<gt-search-facts" in delivered          # delivered this turn
    stem = m._norm_stem("get_user")
    assert m._search_seen[stem]["answered"]          # stamped at delivery
    # repeat -> silent (idempotence via the delivery-time latch)
    out2 = {"output": "a/x.py:1:def get_user(uid):", "returncode": 0}
    m._augment_output({"command": cmd}, out2)
    assert out2["output"] == "a/x.py:1:def get_user(uid):"  # no re-delivery


def test_d4_producer_does_not_self_mark(wired):
    m, _tmp = wired
    # a DIRECT producer call returns the block but must NOT self-stamp the latch
    block = m._search_localize_block("grep -rn get_user .",
                                     "a/x.py:1:def get_user(uid):")
    assert block
    stem = m._norm_stem("get_user")
    e = m._search_seen.get(stem)
    assert e is None or not e.get("answered")  # producer left it un-answered


# ── D-5: Lane-B winner hash stamped at the APPEND, not the gate ─────────────
def test_d5_gate_records_hash_but_does_not_stamp(wired):
    m, _tmp = wired
    m._oracle_delivered_hashes.clear()
    # a single edit-bound candidate -> relevance waived, floor waived -> wins
    win = m._oracle_gate_blocks([(5.0, "test.kind", "steer body text here", True)])
    assert win == "steer body text here"
    assert m._last_gate_winner_hash                        # recorded for the caller
    # D-5: the gate MUST NOT have stamped the delivered-hash (that is the append's job)
    assert m._last_gate_winner_hash not in m._oracle_delivered_hashes


def test_d5_no_winner_clears_recorded_hash(wired):
    m, _tmp = wired
    # first: a winner records a hash
    m._oracle_gate_blocks([(5.0, "k", "body", True)])
    assert m._last_gate_winner_hash
    # then: an empty candidate pool -> no winner -> recorded hash reset (no stale stamp)
    m._oracle_gate_blocks([])
    assert m._last_gate_winner_hash == ""


# ── S-2: band latch consumed ONLY after a NON-EMPTY render ──────────────────
def test_s2_band_latch_armed_on_empty_render(wired, monkeypatch):
    m, _tmp = wired
    monkeypatch.setattr(m, "_GT_STEP_LIMIT", 20, raising=False)
    m._action_count = 5
    m._oracle_edited_rels.add("a/x.py")
    # force a band, force the render EMPTY (targeting unavailable / leak-dropped)
    monkeypatch.setattr(m, "verify_horizon_band", lambda **k: "advisory")
    monkeypatch.setattr(m, "_render_verify_emission", lambda *a, **k: "")
    assert m._horizon_advisory_fired is False
    got = m._verification_horizon_candidate()
    assert got is None
    # the band was NOT spent — an empty render leaves it armed to fire later
    assert m._horizon_advisory_fired is False


def test_s2_band_latch_consumed_on_nonempty_render(wired, monkeypatch):
    m, _tmp = wired
    monkeypatch.setattr(m, "_GT_STEP_LIMIT", 20, raising=False)
    m._action_count = 5
    m._oracle_edited_rels.add("a/x.py")
    monkeypatch.setattr(m, "verify_horizon_band", lambda **k: "advisory")
    monkeypatch.setattr(m, "_render_verify_emission",
                        lambda *a, **k: '\n<gt-verify level="advisory">\nx\n</gt-verify>')
    got = m._verification_horizon_candidate()
    assert got is not None and got[1] == "verify.horizon.advisory"
    assert m._horizon_advisory_fired is True  # consumed on the real render
