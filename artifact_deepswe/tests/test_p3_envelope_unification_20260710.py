"""P3 ENVELOPE UNIFICATION — RL-1 (lane seals) + D-7 (lane budget) + D-2 (16-hex).

RL-1 routes Lane-A/Lane-B deliveries through the ONE EvidenceEnvelope contract (chain
+ dedup stamp-at-seal + receipt ledger) — BOOKKEEPING ONLY, the delivered BYTES are
unchanged. Flag GT_LANE_ENVELOPE, default-off byte-identical (incl. bookkeeping).
"""
from __future__ import annotations

import hashlib
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


_GREP = "grep -rn get_user ."
_GREP_OUT = "a/x.py:1:def get_user(uid):"


# ── D-2: 16-hex delivered hashes, dedup still suppresses a repeat ────────────
def test_d2_content_hash_is_16_hex():
    h = g._oracle_content_hash("some block text")
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)


def test_d2_lane_dedup_still_suppresses_repeat(wired):
    m, _tmp = wired
    out1 = {"output": _GREP_OUT, "returncode": 0}
    m._augment_output({"command": _GREP}, out1)
    assert "<gt-search-facts" in out1["output"]        # delivered
    # second identical grep -> suppressed by the (now 16-hex) dedup / answered latch
    out2 = {"output": _GREP_OUT, "returncode": 0}
    m._augment_output({"command": _GREP}, out2)
    assert out2["output"] == _GREP_OUT                 # no re-delivery


# ── RL-1: default-off byte-identical bookkeeping (no lane seals) ─────────────
def test_rl1_default_off_no_lane_seal(wired, monkeypatch):
    m, _tmp = wired
    monkeypatch.delenv("GT_LANE_ENVELOPE", raising=False)
    assert not m._lane_envelope_on()
    head_before = m._gt_gateway_chain_head
    out = {"output": _GREP_OUT, "returncode": 0}
    m._augment_output({"command": _GREP}, out)
    assert "<gt-search-facts" in out["output"]         # bytes still delivered
    assert m._gt_gateway_deliveries == []              # NO seal (bookkeeping unchanged)
    assert m._gt_gateway_chain_head == head_before     # chain unadvanced


# ── RL-1: flag-on seals the lane delivery through the envelope contract ──────
def test_rl1_on_seals_lane_delivery(wired, monkeypatch):
    m, _tmp = wired
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    assert m._lane_envelope_on()
    delivered_before = len(m._gt_gateway_deliveries)
    out = {"output": _GREP_OUT, "returncode": 0}
    m._augment_output({"command": _GREP}, out)
    delivered = out["output"][len(_GREP_OUT):]
    assert "<gt-search-facts" in delivered             # SAME bytes as off-path
    # a sealed, receipt-tracked delivery was recorded, the chain advanced, and the
    # envelope dedup_key is stamped into the shared delivered-dedup set
    assert len(m._gt_gateway_deliveries) == delivered_before + 1
    sealed = m._gt_gateway_deliveries[-1]
    assert sealed.receipt_state == "delivered"         # level-1 receipt
    assert sealed.evidence_type == "def_partition"
    assert len(sealed.rendered_bytes_hash) == 64       # law-6 delivery evidence
    assert sealed.dedup_key in m._EPISODE.delivered_dedup   # stamp-at-seal
    assert m._gt_gateway_chain_head                     # chain advanced


def test_rl1_on_delivered_bytes_identical_to_off(wired, monkeypatch):
    """RL-1 is bookkeeping ONLY: the agent-visible delta is byte-identical on/off."""
    m, tmp = wired
    monkeypatch.delenv("GT_LANE_ENVELOPE", raising=False)
    out_off = {"output": _GREP_OUT, "returncode": 0}
    m._augment_output({"command": _GREP}, out_off)
    delta_off = out_off["output"][len(_GREP_OUT):]
    # fresh state, flag ON
    m._reset_oracle_state()
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    out_on = {"output": _GREP_OUT, "returncode": 0}
    m._augment_output({"command": _GREP}, out_on)
    delta_on = out_on["output"][len(_GREP_OUT):]
    assert delta_on == delta_off and delta_on != ""    # identical bytes, both non-empty


# ── D-7: over-budget lane block dropped WHOLE (flag on) ─────────────────────
def test_d7_over_budget_lane_block_dropped_whole(wired, monkeypatch):
    m, _tmp = wired
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(m, "_GT_LANE_MAX_DELTA", 10, raising=False)  # tiny cap
    # a Lane-A block far over the cap -> dropped whole, NOT clipped
    out = {"output": "base", "returncode": 0}
    lane_a = [("l3.contract", "\n<gt-contract>" + "X" * 500 + "</gt-contract>")]
    m._lane_a_deliver(out, "cat a/x.py", lane_a, krel="a/x.py", event=None)
    assert out["output"] == "base"   # nothing appended (dropped whole, never clipped)


def test_d7_within_budget_lane_block_delivers(wired, monkeypatch):
    m, _tmp = wired
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    out = {"output": "base", "returncode": 0}
    block = "\n<gt-contract>small</gt-contract>"
    m._lane_a_deliver(out, "cat a/x.py", [("l3.contract", block)],
                      krel="a/x.py", event=None)
    assert out["output"] == "base" + block   # within cap -> delivered verbatim
