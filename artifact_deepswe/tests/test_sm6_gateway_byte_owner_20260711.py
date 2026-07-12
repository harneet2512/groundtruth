r"""SM-6 (A) — Gateway = the ONLY observation-byte owner, EQUIVALENCE-PROVEN under the
SM-5 global arbiter. 2026-07-11.

SM-2b built the per-edit REPLACEMENT-DELIVERS tripwire (``_gt_gateway_caller_contract_ready``)
that retires the legacy Lane-A caller-break strings (``l3.contract`` / ``l3b.evidence``) ONLY
when the Gateway's cross-language ``caller_contract`` producer provably delivers an equivalent
break — and proved that migration INLINE (GT_GATEWAY on, arbiter off) in
``test_rl_lane_a_retirement_20260711``. SM-5 built the ONE global ranked competition (the pool).

SM-6 (A) closes the P0 the plan flags: a legacy Lane-A factual class may die ONLY after its
Gateway envelope replacement is proven to (1) be PRODUCED, (2) WIN arbitration, (3) yield
EQUIVALENT observation bytes — and this must hold UNDER the SM-5 pool, not only inline. This
file is the per-class equivalence MATRIX under ``GT_GLOBAL_ARBITER=1`` + the residual report.

FINDING (empirically, below): the SM-2b retirement composes with the SM-5 pool with NO new
delivery-path code — the retired ``lane_a`` list is stashed into the pool already-filtered, the
Gateway produces the ``caller_break`` into the SAME pool, and the arbiter delivers it as the
single dose. The delivered bytes under the arbiter are BYTE-IDENTICAL to the inline path.

EQUIVALENCE MATRIX (per retired class):
  l3.contract   legacy-produced / envelope=caller_break / WON arbitration / equal bytes  GREEN
  l3b.evidence  legacy-produced / envelope=caller_break / WON arbitration / equal bytes  GREEN
  l3.cochange   INTERNAL (no native form; Gateway delivers nothing)                      GREEN
RESIDUALS (LEFT-LIVE — no proven Gateway replacement, KEEP-legacy, correct-or-quiet):
  consensus.scope_map · post_search.localize · concern.consensus
"""
from __future__ import annotations

import sqlite3

import gt_mini_patch as g
from groundtruth.runtime import gateway as gw
from groundtruth.runtime.native_render import (
    contains_test_identity, render_cochange_native)


# --------------------------------------------------------------------------- #
# Harness — a sig-change edit over a graph with one deterministic cross-file caller.
# --------------------------------------------------------------------------- #
def _mk_graph_with_caller(db):
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,"
        " source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT);"
        "CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,"
                "language) VALUES(1,'Function','foo','pkg/mod.py',1,5,'foo(a, b)',0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,"
                "language) VALUES(2,'Function','bar','pkg/other.py',1,5,'bar()',0,'python')")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,2,1,'CALLS',3,'import',1.0)")
    con.commit()
    con.close()


def _run_sig_change_edit(tmp_path, monkeypatch, *, gateway_on, arbiter_on, bridges_on):
    tmp_path.mkdir(parents=True, exist_ok=True)   # caller may pass a not-yet-created subdir
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text("def foo(a, b, c):\n    return a\n", encoding="utf-8")
    db = str(tmp_path / "graph.db")
    _mk_graph_with_caller(db)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_GATEWAY", "1" if gateway_on else "0")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1" if arbiter_on else "0")
    if bridges_on:
        monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    else:
        monkeypatch.delenv("GT_GATEWAY_EDIT_BRIDGES", raising=False)
    g._reset_oracle_state()
    action = {"command": "str_replace", "path": "pkg/mod.py",
              "old_str": "def foo(a, b):", "new_str": "def foo(a, b, c):"}
    out = {"output": "ok", "returncode": 0}
    g._augment_output(action, out)
    g._reset_oracle_state()
    return out["output"]


# --------------------------------------------------------------------------- #
# (1) SOLE BYTE OWNER — under the arbiter the Gateway caller_break WINS + delivers,
#     and the legacy l3.contract / l3b.evidence are retired (absent).
# --------------------------------------------------------------------------- #
def test_arbiter_gateway_caller_break_is_sole_byte_owner(tmp_path, monkeypatch):
    obs = _run_sig_change_edit(tmp_path, monkeypatch,
                               gateway_on=True, arbiter_on=True, bridges_on=True)
    assert "caller_break" in obs                 # (1) produced (2) WON the pool -> delivered
    assert "signature changed" in obs
    assert "1 caller(s) in 1 file(s)" in obs      # the actionable equivalent of the legacy break
    assert "<gt-contract" not in obs             # legacy l3.contract retired under the arbiter
    assert "<gt-evidence" not in obs             # legacy l3b.evidence retired under the arbiter


# --------------------------------------------------------------------------- #
# (2) EQUAL OBSERVATION BYTES — the arbiter (pool) delivery is byte-identical to the
#     inline (arbiter-off) delivery. The SM-5 collapse loses NO byte of the migration.
# --------------------------------------------------------------------------- #
def test_arbiter_delivery_bytes_equal_inline(tmp_path, monkeypatch):
    inline = _run_sig_change_edit(tmp_path / "a", monkeypatch,
                                  gateway_on=True, arbiter_on=False, bridges_on=True)
    pooled = _run_sig_change_edit(tmp_path / "b", monkeypatch,
                                  gateway_on=True, arbiter_on=True, bridges_on=True)
    assert inline == pooled                       # byte-identical caller_break delivery
    assert inline.startswith("ok")                # base observation preserved (byte-preserving)


# --------------------------------------------------------------------------- #
# (3) NO DARK GAP — GT_GLOBAL_ARBITER=1 + GT_GATEWAY=1 but the replacement CANNOT deliver
#     (bridges off): the legacy KEEPS delivering under the arbiter. §26.4 no-deletion.
#     NOTE: the arbiter is a ONE-dose engine, so when both legacy caller-break blocks
#     (l3.contract + l3b.evidence, same caller_contract class) sit in the pool it COLLAPSES
#     them to one winner (l3.contract, lower seq). "No dark gap" = the legacy caller-break
#     survives (one block), not that both do — the collapse is correct SM-5 behavior.
# --------------------------------------------------------------------------- #
def _legacy_caller_break_present(obs: str) -> bool:
    return "<gt-contract" in obs or "<gt-evidence" in obs


def test_arbiter_no_replacement_keeps_legacy(tmp_path, monkeypatch):
    obs = _run_sig_change_edit(tmp_path, monkeypatch,
                               gateway_on=True, arbiter_on=True, bridges_on=False)
    assert _legacy_caller_break_present(obs)     # legacy survives (no replacement -> no retire)
    assert "<gt-contract" in obs                  # deterministic winner (lower seq than evidence)
    assert "caller_break" not in obs              # the Gateway produced nothing (bridges off)


# --------------------------------------------------------------------------- #
# (4) BYTE-IDENTICAL-OFF — both flags OFF -> the legacy delivers exactly as pre-SM-6.
# --------------------------------------------------------------------------- #
def test_both_flags_off_delivers_legacy(tmp_path, monkeypatch):
    obs = _run_sig_change_edit(tmp_path, monkeypatch,
                               gateway_on=False, arbiter_on=False, bridges_on=False)
    assert "<gt-contract" in obs and "<gt-evidence" in obs
    assert "caller_break" not in obs


def test_arbiter_on_gateway_off_delivers_legacy(tmp_path, monkeypatch):
    """The arbiter alone (no Gateway) has NO caller_break producer in the pool -> the legacy
    Lane-A caller-break is NOT retired (retirement needs GT_GATEWAY) and delivers via the pool
    (arbiter-collapsed to one block — l3.contract wins)."""
    obs = _run_sig_change_edit(tmp_path, monkeypatch,
                               gateway_on=False, arbiter_on=True, bridges_on=False)
    assert _legacy_caller_break_present(obs) and "<gt-contract" in obs
    assert "caller_break" not in obs


# --------------------------------------------------------------------------- #
# (5) LEAK = 0 — the consolidated (sole-owner) delivery carries no test identity.
# --------------------------------------------------------------------------- #
def test_arbiter_delivery_leak_zero(tmp_path, monkeypatch):
    obs = _run_sig_change_edit(tmp_path, monkeypatch,
                               gateway_on=True, arbiter_on=True, bridges_on=True)
    # strip the base observation; the GT delta is what shipped.
    delta = obs[len("ok"):]
    assert not contains_test_identity(delta)


# --------------------------------------------------------------------------- #
# (6) INTERNAL retiree — cochange has NO native form; the Gateway delivers nothing for it.
# --------------------------------------------------------------------------- #
def test_cochange_is_internal_no_native_form():
    assert "l3.cochange" in g._GATEWAY_RETIRED_INTERNAL
    assert render_cochange_native("a/x.py", 3) == ""


# --------------------------------------------------------------------------- #
# (7) RESIDUALS — the un-migratable classes are LEFT-LIVE (no proven Gateway replacement).
# --------------------------------------------------------------------------- #
def test_residual_classes_are_not_retired():
    residuals = {"consensus.scope_map", "post_search.localize", "concern.consensus"}
    assert residuals.isdisjoint(g._GATEWAY_RETIRED_LANE_A)
    # and the predicate never retires them, even with a (spurious) replacement_ready.
    import os
    os.environ["GT_GATEWAY"] = "1"
    g._GT_BASELINE = False
    try:
        for k in residuals:
            assert g._lane_a_retired_under_gateway(k, replacement_ready=True) is False, k
    finally:
        os.environ.pop("GT_GATEWAY", None)


def test_only_the_three_declared_classes_may_retire():
    assert g._GATEWAY_RETIRED_LANE_A == frozenset(
        {"l3.cochange", "l3.contract", "l3b.evidence"})


# --------------------------------------------------------------------------- #
# MUTATIONS — the replacement-delivers gate is load-bearing UNDER THE ARBITER too.
# --------------------------------------------------------------------------- #
def test_mutation_1_silent_producer_keeps_legacy_under_arbiter(tmp_path, monkeypatch):
    """MUTATION 1: force the Gateway caller_contract producer silent WHILE the arbiter is on.
    The tripwire is then False -> the legacy l3.contract / l3b.evidence must STILL deliver via
    the pool (no dark gap). If retirement ignored the tripwire the legacy would vanish here."""
    monkeypatch.setattr(gw, "_produce_caller_contract", lambda ev, st: [])
    obs = _run_sig_change_edit(tmp_path, monkeypatch,
                               gateway_on=True, arbiter_on=True, bridges_on=True)
    assert _legacy_caller_break_present(obs) and "<gt-contract" in obs  # legacy survives silent producer
    assert "caller_break" not in obs


def test_mutation_2_ignoring_tripwire_creates_dark_gap(monkeypatch):
    """MUTATION 2: a retirement predicate that IGNORES ``replacement_ready`` retires the
    legacy even when the replacement is silent -> a DARK GAP (neither legacy nor envelope).
    Proven by a local mutant vs the real predicate, which keeps the legacy when not-ready."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)

    def _mutant(kind, *, replacement_ready=False):
        # dropped the replacement_ready gate -> retires unconditionally under GT_GATEWAY.
        return kind in g._GATEWAY_RETIRED_LANE_A

    assert _mutant("l3.contract", replacement_ready=False) is True    # mutant: dark gap
    # real predicate: replacement NOT ready -> KEEP legacy (no deletion, no dark gap).
    assert g._lane_a_retired_under_gateway("l3.contract", replacement_ready=False) is False
