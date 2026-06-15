"""Fix-campaign regression tests — scope/consensus + oracle steer fail-closed.

UNIT tests of gt_mini_patch.py (NOT a benchmark/agent run). Each is written to
go RED on the pre-fix code and GREEN after the surgical fix. Worst-first:

  B1  delivered <gt-scope> set admits name_match guesses — _query_scope /
      _consensus_block gated on confidence ONLY (>=0.5), so a 0.6 name_match
      edge reached the agent as "graph-connected / in scope". FACTS-ONLY gate
      (resolution_method ∈ DETERMINISTIC) mirrors curation_map._neighbors.
  B2  "you edited X of N in-scope files" — N was the accumulated, polluted
      global scope union; the denominator must be the issue-anchored connected
      component reachable from the EDITED files via VERIFIED edges.
  B3  completeness anchor filter case-broken — unedited members lowercased,
      focus tokens kept original case -> CamelCase anchors never intersect.
  B4  verify/failure/no-test steers phase-filtered out at the test turn and
      latch-burned -> permanently silenced. TEST_RESULT event + test_count
      into the phase state + phase-drop re-arm.
  B5  l3.cochange latch consumed at PRODUCTION, lost on a dedup collision ->
      latch only on a real DELIVERED outcome.
  B6  _ledger_judge_pending judged only the LAST delivery of a multi-block
      turn -> _pending_delivery is a list/set, judge all.
  B7  _consensus_block emits a non-empty <gt-scope> even on zero scope -> "".
  B8  files="N" hardcoded count can disagree with rendered lines; unverified
      neighbours tagged (unverified) in the override wording.
  B9  _record_hook_suppress counts dedup ("delivered") as suppressed ->
      eligible = fired + suppressed over-counts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared: a tiny REAL graph.db with the production nodes/edges schema, holding
# ONE fact edge (resolution_method='import', conf 1.0) and ONE name_match guess
# (resolution_method='name_match', conf 0.6) out of `focus.py`.
# --------------------------------------------------------------------------- #
def _make_scope_graph(path: str, *, with_method: bool = True) -> None:
    con = sqlite3.connect(path)
    if with_method:
        con.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
                language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
                type TEXT, resolution_method TEXT, confidence REAL,
                source_file TEXT, source_line INTEGER
            );
            """
        )
    else:
        # Legacy schema: NO resolution_method column (only confidence).
        con.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
                language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
                type TEXT, confidence REAL
            );
            """
        )
    con.execute("INSERT INTO nodes VALUES (1,'Function','foc','focus.py','python',NULL,0)")
    con.execute("INSERT INTO nodes VALUES (2,'Function','fact_callee','fact_neighbor.py','python',NULL,0)")
    con.execute("INSERT INTO nodes VALUES (3,'Function','guess_callee','guess_neighbor.py','python',NULL,0)")
    if with_method:
        # FACT edge focus.py -> fact_neighbor.py (import, conf 1.0)
        con.execute(
            "INSERT INTO edges VALUES (NULL,1,2,'CALLS','import',1.0,'focus.py',10)"
        )
        # name_match GUESS focus.py -> guess_neighbor.py (conf 0.6, ABOVE the 0.5 gate)
        con.execute(
            "INSERT INTO edges VALUES (NULL,1,3,'CALLS','name_match',0.6,'focus.py',11)"
        )
    else:
        con.execute("INSERT INTO edges VALUES (NULL,1,2,'CALLS',1.0)")
        con.execute("INSERT INTO edges VALUES (NULL,1,3,'CALLS',0.6)")
    con.commit()
    con.close()


@pytest.fixture
def scope_db(monkeypatch, tmp_path):
    db = str(tmp_path / "graph.db")
    _make_scope_graph(db, with_method=True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    # clean module-global scope/consensus state for each test
    g._consensus_scope.clear()
    g._oracle_edited_rels.clear()
    g._seen.clear()
    monkeypatch.setattr(g, "_consensus_fired", False)
    monkeypatch.setattr(g, "_offscope_views", 0)
    return db


@pytest.fixture
def scope_db_legacy(monkeypatch, tmp_path):
    db = str(tmp_path / "graph_legacy.db")
    _make_scope_graph(db, with_method=False)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    g._consensus_scope.clear()
    return db


# --------------------------------------------------------------------------- #
# B1 — delivered scope set is FACTS-ONLY (no name_match guess).
# --------------------------------------------------------------------------- #
def test_b1_query_scope_drops_name_match(scope_db):
    """RED pre-fix: the 0.6 name_match neighbour passed the conf>=0.5 gate and
    was returned as scope. GREEN: only the FACT (import) neighbour survives."""
    out = g._query_scope("focus.py")
    assert any("fact_neighbor.py" in p for p in out), out
    assert not any("guess_neighbor.py" in p for p in out), (
        "name_match guess laundered into delivered scope: %r" % out
    )


def test_b1_consensus_block_scope_has_no_name_match_file(scope_db):
    """The first-view <gt-scope> must NOT list the name_match neighbour."""
    block = g._consensus_block("focus.py", "/repo")
    assert "fact_neighbor" in block, block
    assert "guess_neighbor" not in block, (
        "name_match file rendered in <gt-scope>: %r" % block
    )


def test_b1_legacy_db_verified_only_floor(scope_db_legacy):
    """No resolution_method column -> cannot judge provenance; the conf gate is
    raised to a verified-only floor so a 0.6 edge is NOT delivered as scope."""
    out = g._query_scope("focus.py")
    # 1.0 fact neighbour survives; 0.6 (cannot prove it is a fact) does not.
    assert any("fact_neighbor.py" in p for p in out), out
    assert not any("guess_neighbor.py" in p for p in out), (
        "legacy DB delivered a 0.6 edge as scope (no verified-only floor): %r" % out
    )


# --------------------------------------------------------------------------- #
# B7 — empty scope -> empty block (not a one-line non-empty <gt-scope>).
# --------------------------------------------------------------------------- #
def test_b7_consensus_block_empty_on_zero_scope(monkeypatch, tmp_path):
    """A file with NO verified neighbours yields no <gt-scope> at all."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            file_path TEXT, language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL, source_file TEXT, source_line INTEGER);
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','lonely','lonely.py','python',NULL,0)")
    con.commit()
    con.close()
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_fired", False)
    g._consensus_scope.clear()
    block = g._consensus_block("lonely.py", "/repo")
    assert block == "", "non-empty <gt-scope> on zero scope: %r" % block


# --------------------------------------------------------------------------- #
# B8 — files="N" equals the number of rendered lines.
# --------------------------------------------------------------------------- #
def test_b8_files_attr_matches_rendered_lines(scope_db):
    block = g._consensus_block("focus.py", "/repo")
    if not block:
        pytest.skip("no scope -> covered by B7")
    import re as _re
    m = _re.search(r'files="(\d+)"', block)
    assert m, block
    declared = int(m.group(1))
    rendered = len([ln for ln in block.splitlines() if _re.match(r"\s*\d+\.", ln)])
    assert declared == rendered, (
        "files=%d disagrees with %d rendered lines: %r" % (declared, rendered, block)
    )


# --------------------------------------------------------------------------- #
# B3 — completeness anchor filter is case-insensitive (CamelCase anchors).
# --------------------------------------------------------------------------- #
def test_b3_completeness_camelcase_anchor_intersects(monkeypatch):
    """RED pre-fix: focus={'ParseConfig'} kept original case, unedited members
    lowercased -> never intersect -> the completeness nudge never fired for a
    CamelCase-anchored unedited scope member. GREEN: case-folded intersection."""
    g._consensus_scope.clear()
    g._oracle_edited_rels.clear()
    # edited one file; an unedited scope member named after a CamelCase anchor.
    g._consensus_scope.update({"src/edited.py", "src/parseconfig.py"})
    g._oracle_edited_rels.add("src/edited.py")
    monkeypatch.setattr(g, "_oracle_focus", lambda: {"ParseConfig"})
    # Force B2's component restriction to be a no-op for this case test by making
    # the verified-component equal the whole scope (the focus/case axis is isolated).
    monkeypatch.setattr(g, "_verified_scope_component", lambda edited: set(g._consensus_scope))
    block = g._scope_completeness_block()
    assert "parseconfig.py" in block, (
        "CamelCase anchor did not intersect lowercased unedited member: %r" % block
    )


# --------------------------------------------------------------------------- #
# B2 — completeness denominator is the verified component of EDITED files,
# not the accumulated global scope grab-bag.
# --------------------------------------------------------------------------- #
def test_b2_denominator_is_verified_component_not_global_union(monkeypatch):
    """RED pre-fix: N = len(_consensus_scope) (every viewed file's neighbourhood).
    GREEN: N = the component reachable from the EDITED files via verified edges."""
    g._consensus_scope.clear()
    g._oracle_edited_rels.clear()
    # Global accumulated scope has 5 unrelated files (viewed-file pollution)...
    g._consensus_scope.update({
        "src/edited.py", "src/related.py",
        "src/unrelated_a.py", "src/unrelated_b.py", "src/unrelated_c.py",
    })
    g._oracle_edited_rels.add("src/edited.py")
    # ...but only edited.py + related.py are in the verified component of the edits.
    monkeypatch.setattr(
        g, "_verified_scope_component",
        lambda edited: {"src/edited.py", "src/related.py"},
    )
    monkeypatch.setattr(g, "_oracle_focus", lambda: {"related"})
    block = g._scope_completeness_block()
    assert block, "expected a completeness nudge"
    # denominator must be 2 (the verified component), NOT 5 (the global union).
    assert "of 2 graph-connected" in block, (
        "denominator is the polluted global union, not the verified component: %r" % block
    )


# --------------------------------------------------------------------------- #
# B5 — l3.cochange latch is consumed only on a real DELIVERED outcome.
# --------------------------------------------------------------------------- #
def test_b5_cochange_latch_survives_dedup_collision(monkeypatch):
    """RED pre-fix: _cochange_block set _cochange_fired=True at production; if the
    block content-hash was already delivered (dedup collision in _lane_a_deliver),
    the latch burned with NO delivery -> the co-change completeness signal was lost.
    GREEN: the producer does NOT set the latch; _lane_a_deliver sets it only on a
    real append."""
    monkeypatch.setattr(g, "_cochange_fired", False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    # Make _cochange_block return a fixed non-empty block without a DB.
    text = "\n<gt-cochange>\n- a/b.py (co-changed 3x)\n</gt-cochange>"
    monkeypatch.setattr(g, "_cochange_block", lambda rel: text)
    # Pre-seed the delivered-hash set so this exact block dedups (collision).
    g._oracle_delivered_hashes.clear()
    h = g._oracle_content_hash(text)
    g._oracle_delivered_hashes.add(h)
    out = {"output": ""}
    g._lane_a_deliver(out, "sed -i s/x/y/ a/b.py",
                      [("l3.cochange", text)], krel="a/b.py", event=None)
    # The block was deduped (not appended) -> the latch must NOT have burned.
    assert g._cochange_fired is False, (
        "cochange latch burned on a dedup collision (no delivery) -> signal lost"
    )


def test_b5_cochange_latch_set_on_real_delivery(monkeypatch):
    """A real (non-dedup) delivery DOES set the latch (fire-once contract holds)."""
    monkeypatch.setattr(g, "_cochange_fired", False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    text = "\n<gt-cochange>\n- a/c.py (co-changed 4x)\n</gt-cochange>"
    g._oracle_delivered_hashes.clear()
    out = {"output": ""}
    g._lane_a_deliver(out, "sed -i s/x/y/ a/b.py",
                      [("l3.cochange", text)], krel="a/b.py", event=None)
    assert text in out["output"], "real cochange delivery did not reach the agent"
    assert g._cochange_fired is True, "latch not set on a real delivery"


# --------------------------------------------------------------------------- #
# B6 — _ledger_judge_pending judges EVERY delivery of a multi-block turn.
# --------------------------------------------------------------------------- #
def test_b6_judge_all_deliveries_of_a_turn(monkeypatch):
    """RED pre-fix: _pending_delivery was a single tuple, overwritten by the last
    note; an earlier kind delivered the SAME turn was never judged (mis-attributed
    consumption). GREEN: every kind noted this turn is judged against next cmd."""
    g._ledger_consumed_kinds.clear()
    g._ledger_ignore_counts.clear()
    g._reset_pending_delivery()
    # Two blocks delivered the same turn.
    g._ledger_note_delivery("l3.contract", "cat focus.py")
    g._ledger_note_delivery("l3b.evidence", "cat focus.py")
    # Next turn: an EDIT command (consumption signal) -> BOTH must be credited.
    g._ledger_judge_pending("sed -i s/a/b/ focus.py")
    assert "l3.contract" in g._ledger_consumed_kinds, (
        "first delivery of a multi-block turn was not judged (overwritten)"
    )
    assert "l3b.evidence" in g._ledger_consumed_kinds


# --------------------------------------------------------------------------- #
# B9 — dedup ("delivered") is NOT counted as a suppression.
# --------------------------------------------------------------------------- #
def test_b9_dedup_not_counted_as_suppress(monkeypatch, tmp_path):
    """RED pre-fix: a dedup (reason='delivered') incremented '<kind>.suppressed'
    -> eligible = fired + suppressed over-counted. GREEN: dedup is recorded under
    a distinct '<kind>.deduped' key (or skipped), never '.suppressed'."""
    monkeypatch.setattr(g, "_HOOK_FIRE_COUNTS", {})
    monkeypatch.setenv("GT_HOOK_FIRE_COUNTS", str(tmp_path / "fc.json"))
    g._record_hook_suppress("l5.failure", reason="delivered")
    assert g._HOOK_FIRE_COUNTS.get("l5.failure.suppressed", 0) == 0, (
        "dedup was counted under .suppressed (over-counts eligible)"
    )
    # a genuine suppression (e.g. outranked) IS counted under .suppressed.
    g._record_hook_suppress("l5.failure", reason="outranked")
    assert g._HOOK_FIRE_COUNTS.get("l5.failure.suppressed", 0) == 1


# --------------------------------------------------------------------------- #
# B4 — TEST_RESULT event + test_count reaches VERIFY; phase-drop re-arms the latch.
# --------------------------------------------------------------------------- #
def test_b4_current_event_returns_test_result_on_test_command():
    """RED pre-fix: _current_event never returned TEST_RESULT. GREEN: a real
    test-runner command maps to Event.TEST_RESULT (mirrors command_event)."""
    ev = g._current_event_for_cmd("pytest tests/test_x.py")
    assert ev == g.Event.TEST_RESULT, ev


def test_b4_phase_reaches_verify_on_first_test(monkeypatch):
    """RED pre-fix: _detect_phase omitted test_count, so a test turn with
    nonedit_streak<3 stayed EDIT. GREEN: test evidence -> VERIFY on the first test."""
    monkeypatch.setattr(g, "_action_count", 10)
    monkeypatch.setattr(g, "_GT_STEP_LIMIT", 300)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("src/x.py")
    monkeypatch.setattr(g, "_source_edit_count", 1)
    monkeypatch.setattr(g, "_oracle_nonedit_streak", 1)  # < 3
    monkeypatch.setattr(g, "_oracle_test_count", 1)      # a test WAS observed
    phase = g._detect_phase()
    assert phase == g.Phase.VERIFY, (
        "phase did not reach VERIFY on the first test (test_count ignored): %r" % phase
    )


def test_b4_phase_drop_rearms_fire_once_latch(monkeypatch):
    """RED pre-fix: a fire-once steer dropped by the phase filter never re-armed
    (only gate losers re-arm). GREEN: the phase filter records the dropped kind so
    the existing re-arm restores its latch (deferred, not destroyed)."""
    g._reset_phase_dropped_losers()
    # A fire-once steer that the phase policy disallows in the current phase.
    cands = [(5.0, "l5.failure", "<x>steer</x>", True)]
    # Drive the filter in a phase where l5.failure is NOT allowed. EDIT phase
    # disallows the verify-axis steers (that is the whole bug).
    kept = g._filter_candidates_by_phase(cands, g.Phase.EDIT, g.Event.POST_EDIT,
                                         file_path="src/x.py")
    if "l5.failure" in {k for _s, k, _t, _e in kept}:
        pytest.skip("phase policy admits l5.failure in EDIT — covered by other tests")
    assert "l5.failure" in g._phase_dropped_losers, (
        "phase-dropped fire-once steer not staged for re-arm -> permanently silenced"
    )
    # The staging set MUST survive a gate run: _oracle_gate_blocks resets
    # _oracle_last_losers to set(); the merge wiring in _augment_output unions
    # _phase_dropped_losers back in AFTER the gate. Prove the staging survives the
    # gate reset (so the merge has something to restore) — pins the merge as
    # load-bearing, not just the staging.
    g._oracle_delivered_hashes.clear()
    g._oracle_gate_blocks([])  # resets _oracle_last_losers to set()
    # The staging set MUST survive the gate reset — that is precisely why it is a
    # SEPARATE set from _oracle_last_losers (the gate clears the latter at its top).
    # If this fails, the post-gate merge has nothing to restore and the latch stays
    # burned -> permanent silence (the bug).
    assert "l5.failure" in g._phase_dropped_losers, (
        "gate reset clobbered the phase-drop staging -> the re-arm merge can never "
        "restore the latch"
    )


def test_b4_l5_failure_steer_reaches_agent_on_test_turn(monkeypatch, tmp_path):
    """END-TO-END behavioral pin (drives the REAL _augment_output): a persisted
    test failure across an edit must deliver the l5.failure steer to the agent.

    RED pre-fix: the producing turn is a test-runner command; _current_event
    classified it as POST_EDIT/None and _detect_phase (no test_count) returned
    EDIT, so the phase filter dropped l5.failure as wrong_phase -> the steer never
    reached out['output']. GREEN: TEST_RESULT event + VERIFY phase admit it."""
    # Minimal anchors so focus is non-empty (the steer is edit_bound anyway).
    anchors = tmp_path / "anchors.json"
    anchors.write_text('{"symbols": ["frob"]}', encoding="utf-8")
    db = tmp_path / "graph.db"
    _make_scope_graph(str(db), with_method=True)
    monkeypatch.setenv("GT_ANCHORS_PATH", str(anchors))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.delenv("GT_BASELINE", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_oracle_focus_cache", None)
    monkeypatch.setattr(g, "_oblig_syms_cache", None)
    g._reset_oracle_state()

    fail_out = "FAILED tests/test_frob.py::test_frob - AssertionError: 1 != 2\n1 failed"

    # Turn 1: a real source edit (so _source_edit_count >= 1).
    g._augment_output({"command": "sed -i '1s/a/b/' src/frob.py"}, {"output": "patched"})
    # Turn 2: test fails (records the sig; first occurrence, not yet persisted).
    g._augment_output({"command": "pytest tests/test_frob.py"}, {"output": fail_out})
    # Turn 3: SAME failure persists across the edit -> l5.failure is produced.
    out3 = {"output": fail_out}
    g._augment_output({"command": "pytest tests/test_frob.py"}, out3)

    assert "failure_persisted" in out3["output"], (
        "l5.failure steer did NOT reach the agent on the test turn — it was "
        "phase-dropped (the silencing bug). Delivered: %r" % out3["output"]
    )
