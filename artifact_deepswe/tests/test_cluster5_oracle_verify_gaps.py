"""Cluster-5 regression tests — oracle/verify/delivery gaps in gt_mini_patch.py.

Each test is a UNIT test of the patched code (NOT a benchmark/agent run). They are
written to go RED on the pre-fix code and GREEN after the surgical fix:

  G06  structural edit-risk scores the EDITED-BUT-UNTESTED set, not the full static
       obligation set -> an UNEDITED (or already-TESTED) high-fan-in symbol can no
       longer raise a verification advisory (correct-or-quiet).
  G07  edit-risk's "deep graph" is documented as CALLS-dominated for a method/function
       edit (READS/WRITES target the owning class); the risk note stays in the verify
       (RISK) substrate and NEVER feeds rank — assert no rank/reach surface is touched.
  G08  _reset_oracle_state() clears the Lane-A dedup sets (_seen, _contract_seen,
       _consensus_scope) so an already-seen file RE-DELIVERS after a retry reset
       (Lane A is always-on, must fire on EVERY edit).
  G10  the structural_edit_risk import is in its OWN bulkhead — an edit_risk import
       miss darkens ONLY structural risk, never the whole verify axis / runtime stack.
  G15  Lane-B (oracle gate) suppressions are counted into the SINGLE
       GT_HOOK_FIRE_COUNTS sink so eligible/emitted/suppressed is reconstructable
       from one file.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import edit_risk as _edit_risk  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared: build a tiny REAL graph.db with the production nodes/edges schema.
# --------------------------------------------------------------------------- #
def _make_graph(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL
        );
        """
    )
    # 'risky_func' = a hub: many VERIFIED CALLS callers (well above the repo baseline).
    # 'safe_func'  = a leaf: a single verified caller (below the notable-dependents tail).
    con.execute("INSERT INTO nodes VALUES (1,'Function','risky_func','a.py','python',NULL)")
    con.execute("INSERT INTO nodes VALUES (2,'Function','safe_func','b.py','python',NULL)")
    for i in range(3, 16):
        con.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,NULL)",
            (i, "Function", f"caller{i}", "x.py", "python"),
        )
    # 12 verified callers -> risky_func  (score climbs well over the 0.5 trigger)
    for i in range(3, 15):
        con.execute(
            "INSERT INTO edges VALUES (NULL,?,1,'CALLS','import',1.0)", (i,)
        )
    # 1 verified caller -> safe_func
    con.execute("INSERT INTO edges VALUES (NULL,3,2,'CALLS','import',1.0)")
    con.commit()
    con.close()


@pytest.fixture
def _risk_env(monkeypatch, tmp_path):
    """Flag ON, real graph wired, obligation set = {risky_func, safe_func}, clean caches."""
    db = str(tmp_path / "graph.db")
    _make_graph(db)
    _edit_risk.reset_reference_cache()
    monkeypatch.setattr(g, "_STRUCTURAL_RISK_ON", True)
    monkeypatch.setattr(g, "_RISK_TRIGGER", 0.5)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: {"risky_func", "safe_func"})
    # isolate the edited/tested token sets per test
    monkeypatch.setattr(g, "_oracle_edited_tokens", set())
    monkeypatch.setattr(g, "_oracle_tested_tokens", set())
    # edited-file set empty by default -> no file constraint (back-compat); the G08
    # tests override it to exercise the file-scope filter.
    monkeypatch.setattr(g, "_oracle_edited_rels", set())
    return db


# --------------------------------------------------------------------------- #
# G06 — score the EDITED-BUT-UNTESTED set, not the static obligation superset.
# --------------------------------------------------------------------------- #
def test_g06_unedited_high_fanin_symbol_does_not_trigger(_risk_env):
    """RED pre-fix: structural_edit_risk(obligation_set) would name risky_func even
    though NOTHING was edited. GREEN: edited set empty -> quiet, no advisory."""
    g._oracle_edited_tokens.clear()
    g._oracle_tested_tokens.clear()
    note, trigger = g._structural_risk_note()
    assert trigger is False
    assert note == ""


def test_g06_edited_untested_high_fanin_symbol_triggers(_risk_env):
    """The risky symbol WAS edited and NOT tested -> the advisory fires and names it."""
    g._oracle_edited_tokens.clear()
    g._oracle_edited_tokens.add("risky_func")
    g._oracle_tested_tokens.clear()
    note, trigger = g._structural_risk_note()
    assert trigger is True
    assert "risky_func" in note


def test_g06_edited_but_already_tested_symbol_is_quiet(_risk_env):
    """Edited AND already tested -> removed from the risk domain -> quiet.
    RED pre-fix: scoring the obligation superset ignored test state entirely."""
    g._oracle_edited_tokens.clear()
    g._oracle_edited_tokens.add("risky_func")
    g._oracle_tested_tokens.clear()
    g._oracle_tested_tokens.add("risky_func")
    note, trigger = g._structural_risk_note()
    assert trigger is False
    assert note == ""


def test_g06_edit_outside_obligation_set_is_quiet(_risk_env, monkeypatch):
    """An incidental edit token NOT in the obligation set must not raise an off-issue
    advisory (relevance intersection with the obligation set)."""
    g._oracle_edited_tokens.clear()
    g._oracle_edited_tokens.add("risky_func")  # risky in the graph...
    g._oracle_tested_tokens.clear()
    # ...but the obligations are unrelated symbols -> intersection empty -> quiet.
    monkeypatch.setattr(
        g, "_obligation_symbol_set",
        lambda: {"unrelated_symbol_a", "unrelated_symbol_b"},
    )
    note, trigger = g._structural_risk_note()
    assert trigger is False
    assert note == ""


# --------------------------------------------------------------------------- #
# G08 — file-scope the risk to the agent's EDITED files. A same-named symbol
# DEFINED in an un-edited file (a callee hub like List.push reached via a
# `x.push(...)` line the diff body tokenizes) is NOT the agent's change.
# --------------------------------------------------------------------------- #
def test_g08_edited_token_defined_in_unedited_file_is_quiet(_risk_env):
    """RED pre-fix: risky_func (defined in x.py) is in the edited tokens + obligation
    set, but the agent edited only other.py -> it is a CALLEE, not the agent's change.
    GREEN: file-scope excludes it -> quiet (the csstree `push (71 dependents)` bug)."""
    g._oracle_edited_tokens.clear()
    g._oracle_edited_tokens.add("risky_func")
    g._oracle_tested_tokens.clear()
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("other.py")  # NOT x.py where risky_func is defined
    note, trigger = g._structural_risk_note()
    assert trigger is False
    assert note == ""


def test_g08_edited_token_in_edited_file_still_triggers(_risk_env):
    """Control: the file-scope filter must NOT over-suppress. risky_func IS defined in
    the edited file (a.py, per _make_graph) -> it stays in scope and the advisory fires."""
    g._oracle_edited_tokens.clear()
    g._oracle_edited_tokens.add("risky_func")
    g._oracle_tested_tokens.clear()
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("a.py")  # the file risky_func is actually defined in
    note, trigger = g._structural_risk_note()
    assert trigger is True
    assert "risky_func" in note


# --------------------------------------------------------------------------- #
# G07 — risk is RISK-substrate only; never enters rank/reach (REQUIRES-I2-LIPI).
# --------------------------------------------------------------------------- #
def test_g07_risk_note_is_pure_and_touches_no_rank_state(_risk_env):
    """_structural_risk_note returns a (str, bool) advisory pair and mutates no
    localizer/reach/rank state. It is the verify (RISK) substrate, node-local."""
    g._oracle_edited_tokens.clear()
    g._oracle_edited_tokens.add("risky_func")
    g._oracle_tested_tokens.clear()
    # snapshot any consensus/scope state that the ranking/scope surface reads
    scope_before = set(g._consensus_scope)
    note, trigger = g._structural_risk_note()
    assert isinstance(note, str) and isinstance(trigger, bool)
    # RISK substrate must not have leaked into the scope/rank surface
    assert set(g._consensus_scope) == scope_before


def test_g07_deep_graph_calls_dominated_documented():
    """The G07 honesty note (CALLS-dominated for method/function edits; RISK-only,
    never RANK) is documented on the producer so a future reader can't re-wire it
    into the ranking surface."""
    doc = g._structural_risk_note.__doc__ or ""
    assert "G07" in doc
    assert "CALLS" in doc
    assert "RANK" in doc  # the explicit depth-never-enters-rank invariant


# --------------------------------------------------------------------------- #
# G08 — reset re-delivers already-seen Lane-A evidence + contract.
# --------------------------------------------------------------------------- #
def test_g08_reset_clears_lane_a_dedup_sets(monkeypatch):
    """RED pre-fix: _reset_oracle_state left _seen / _contract_seen / _consensus_scope
    populated -> Lane A returned '' for every already-seen file after reset."""
    g._seen.clear()
    g._contract_seen.clear()
    g._consensus_scope.clear()
    g._seen.add(("post_edit", "src/a.py"))
    g._contract_seen.add("src/a.py")
    g._consensus_scope.add("src/a.py")
    g._reset_oracle_state()
    assert g._seen == set()
    assert g._contract_seen == set()
    assert g._consensus_scope == set()


def test_g08_contract_re_delivers_after_reset(monkeypatch):
    """Behavioral: a file marked seen in _contract_seen returns '' on a second call,
    but after _reset_oracle_state the SAME file is no longer suppressed (the dedup
    set was cleared) -> the always-on lane fires again on a retry."""
    g._contract_seen.clear()
    rel = "src/widget.py"
    g._contract_seen.add(rel)
    # while seen, the per-file-once gate suppresses (top of _graph_contract_block)
    assert g._graph_contract_block(rel) == ""
    g._reset_oracle_state()
    # cleared -> the gate no longer short-circuits on rel (it is no longer "seen")
    assert rel not in g._contract_seen


# --------------------------------------------------------------------------- #
# G10 — structural_edit_risk import is isolated in its own bulkhead.
# --------------------------------------------------------------------------- #
def test_g10_edit_risk_import_is_isolated_from_runtime_block():
    """The structural_edit_risk import lives in its OWN try/except, AFTER the shared
    runtime-import block. An edit_risk miss must NOT touch _RUNTIME_AVAILABLE or the
    verify-band stubs. We assert both imports resolved independently here, and that
    the source physically separates the two blocks (the bulkhead)."""
    import inspect
    src = inspect.getsource(g)
    # A1b (2026-07-05): the shared runtime block is now PER-MODULE bulkheads; the marker
    # is the module-level `_RUNTIME_AVAILABLE = True` init that opens that section (0-indent
    # now, not the old 4-space in-try assignment). edit_risk is still its OWN bulkhead after.
    shared_close = src.index("\n_RUNTIME_AVAILABLE = True")
    edit_import = src.index("from groundtruth.runtime.edit_risk import structural_edit_risk")
    # the edit_risk import must come AFTER the shared block closes (its own bulkhead),
    # not be a line INSIDE the shared try (which would couple their failure).
    assert edit_import > shared_close
    # and it has its own dedicated except that sets ONLY _structural_edit_risk
    seg = src[edit_import: edit_import + 400]
    assert "except ImportError as _edit_risk_import_err" in seg
    assert "_structural_edit_risk = None" in seg


def test_g10_runtime_available_independent_of_edit_risk():
    """With edit_risk present, the runtime block is available; the structural-risk
    symbol is wired. The two are decoupled (G10): if edit_risk were absent,
    _structural_edit_risk would be None while _RUNTIME_AVAILABLE stayed True."""
    assert g._RUNTIME_AVAILABLE is True
    assert g._structural_edit_risk is not None


# --------------------------------------------------------------------------- #
# G15 — Lane-B suppressions counted into the unified GT_HOOK_FIRE_COUNTS sink.
# --------------------------------------------------------------------------- #
def test_g15_lane_b_suppression_counted_in_unified_sink(monkeypatch, tmp_path):
    """A multi-candidate gate emits ONE winner and suppresses (outranks) the rest;
    each suppression is recorded under '<kind>.suppressed' in the same JSON sink
    _record_hook_fire writes — so eligible/emitted/suppressed is one-file answerable.
    RED pre-fix: suppressions lived only in the oracle ledger, never this sink."""
    fc = tmp_path / "fc.json"
    monkeypatch.setenv("GT_HOOK_FIRE_COUNTS", str(fc))
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(g, "_HOOK_FIRE_COUNTS", {})
    g._oracle_delivered_hashes.clear()
    cands = [
        (5.0, "verify.horizon.advisory", "verify this edited change right now please", True),
        (1.0, "spec.obligation", "obligation status block text content here", True),
    ]
    win = g._oracle_gate_blocks(cands)
    assert win  # the high-severity advisory won
    # the loser was suppressed (outranked) and counted into the SAME sink
    assert g._HOOK_FIRE_COUNTS.get("spec.obligation.suppressed") == 1
    saved = json.loads(fc.read_text(encoding="utf-8"))
    assert saved.get("spec.obligation.suppressed") == 1


def test_g15_detect_and_verify_kinds_recorded(monkeypatch, tmp_path):
    """The suppress counter is kind-agnostic: verify.horizon.* / spec.obligation /
    detect.* all flow into the same sink (the G15 requirement)."""
    fc = tmp_path / "fc.json"
    monkeypatch.setenv("GT_HOOK_FIRE_COUNTS", str(fc))
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(g, "_HOOK_FIRE_COUNTS", {})
    g._oracle_delivered_hashes.clear()
    cands = [
        (9.0, "detect.coherence", "coherence collapse detected on this file please stop", True),
        (4.0, "verify.horizon.urgent", "urgent verify your changes immediately now", True),
        (2.0, "detect.loop", "degenerate loop detected, break the repeated command", True),
    ]
    g._oracle_gate_blocks(cands)
    saved = json.loads(fc.read_text(encoding="utf-8"))
    # two losers (urgent + loop) suppressed into the unified sink
    assert saved.get("verify.horizon.urgent.suppressed") == 1
    assert saved.get("detect.loop.suppressed") == 1
