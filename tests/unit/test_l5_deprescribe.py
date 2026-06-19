"""C6A: L5 hooks + governor advisories must be DIAGNOSTIC, never prescriptive.

Research basis: SWE-PRM (NeurIPS 2025, arXiv:2509.02360) — prescriptive
mid-trajectory feedback ("Next action: run X", "edit Y") LOWERS agent success
versus stating the verifiable observation and letting the agent decide.

This test enumerates EVERY L5 hook builder and the two governor advisory
builders, drives each into its firing condition, and asserts:

  (1) DEFECT-CLOSED: the returned text contains no ``Next action:`` line and
      no line that opens with an imperative verb
      (run/edit/make/verify/focus/inspect/revise/change/fix/stop/repair).

  (2) NEGATIVE CONTROL (no over-suppression): each hook still returns its
      diagnostic ``[GT L5: ...]`` header — de-prescribing must not blank the
      hook out. A silenced hook that returns None or empty is NOT acceptable;
      it must still state the fact it detected.

Both directions are required: defect closed AND content preserved.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile

import pytest

from groundtruth.trajectory import hooks
from groundtruth.trajectory.governor import L5Governor
from groundtruth.trajectory.parsers import FailureRecord
from groundtruth.state.agent_state import L5TrajectoryState, IterationBand


# Imperative verbs that mark a prescriptive command. A diagnostic statement of
# fact never opens a line with one of these.
_BANNED_LEADING_VERBS = {
    "run", "edit", "make", "verify", "focus", "inspect",
    "revise", "change", "fix", "stop", "repair", "start",
    "do", "use", "read", "open",
}

_FIRST_WORD = re.compile(r"[A-Za-z']+")


def _assert_diagnostic(text: str, label: str) -> None:
    """Assert `text` is diagnostic: no 'Next action:' and no leading imperative."""
    assert text, f"{label}: expected non-empty diagnostic text"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        assert not line.lower().startswith("next action:"), (
            f"{label}: prescriptive 'Next action:' line survived: {line!r}"
        )
        m = _FIRST_WORD.match(line)
        if not m:
            continue
        first = m.group(0).lower()
        assert first not in _BANNED_LEADING_VERBS, (
            f"{label}: line opens with imperative verb {first!r}: {line!r}"
        )


def _assert_has_header(text: str, label: str) -> None:
    """NEGATIVE CONTROL: de-prescribe must not blank the hook — header survives."""
    assert "[GT L5:" in text, (
        f"{label}: diagnostic header '[GT L5: ...]' missing — "
        f"de-prescribe over-suppressed the hook. Got: {text!r}"
    )


def _failure() -> FailureRecord:
    return FailureRecord(
        command_kind="pytest",
        failure_kind="assertion",
        failing_unit="test_widget::test_render",
        assertion_or_error="assert 1 == 2",
        expected="2",
        actual="1",
        exception_type="AssertionError",
        top_project_frame="src/widget.py:42",
        raw_excerpt="AssertionError: assert 1 == 2",
    )


def _state(**overrides) -> L5TrajectoryState:
    st = L5TrajectoryState()
    st.instance_id = "deprescribe-test"
    st.max_iter = 100
    st.current_iter = 50
    st.band = IterationBand.MID_COMMITMENT
    for k, v in overrides.items():
        setattr(st, k, v)
    return st


# --------------------------------------------------------------------------- #
# hooks.py — every builder, in both bands where wording branches on band.
# --------------------------------------------------------------------------- #

def test_hook_no_durable_source_progress_finalization():
    st = _state(band=IterationBand.FINALIZATION, current_iter=95)
    msg = hooks.hook_no_durable_source_progress(st, "reproduce_bug.py")
    _assert_diagnostic(msg, "no_durable_source_progress[FINALIZATION]")
    _assert_has_header(msg, "no_durable_source_progress[FINALIZATION]")


def test_hook_no_durable_source_progress_normal():
    st = _state()
    msg = hooks.hook_no_durable_source_progress(st, "reproduce_bug.py")
    _assert_diagnostic(msg, "no_durable_source_progress[normal]")
    _assert_has_header(msg, "no_durable_source_progress[normal]")


def test_hook_premature_commitment():
    st = _state()
    msg = hooks.hook_premature_commitment(
        st, "src/widget.py", confirming_edges_opened=0,
        l3_contract_line="returns Optional[Widget]",
    )
    _assert_diagnostic(msg, "premature_commitment")
    _assert_has_header(msg, "premature_commitment")


def test_hook_patch_hypothesis():
    st = _state()
    msg = hooks.hook_patch_hypothesis(
        st, "src/widget.py", l3_contract_line="returns Optional[Widget]",
    )
    _assert_diagnostic(msg, "patch_hypothesis")
    _assert_has_header(msg, "patch_hypothesis")


def test_hook_hypothesis_falsified():
    st = _state(
        edited_source_files=["src/widget.py"],
        has_source_edit_before_last_failure=True,
    )
    msg = hooks.hook_hypothesis_falsified(
        st, _failure(), l3_contract_line="returns Optional[Widget]",
    )
    _assert_diagnostic(msg, "hypothesis_falsified")
    _assert_has_header(msg, "hypothesis_falsified")


def test_hook_hypothesis_falsified_late_repair():
    # Late-repair suffix path: previously appended an imperative.
    st = _state(
        band=IterationBand.LATE_REPAIR,
        current_iter=85,
        edited_source_files=["src/widget.py"],
        has_source_edit_before_last_failure=True,
    )
    msg = hooks.hook_hypothesis_falsified(st, _failure())
    _assert_diagnostic(msg, "hypothesis_falsified[late_repair]")
    _assert_has_header(msg, "hypothesis_falsified[late_repair]")


def test_hook_same_failure_persisted():
    st = _state(
        edited_source_files=["src/widget.py"],
        repeated_failure_count=2,
    )
    msg = hooks.hook_same_failure_persisted(
        st, _failure(), l3_repair_line="guard the None branch",
    )
    _assert_diagnostic(msg, "same_failure_persisted")
    _assert_has_header(msg, "same_failure_persisted")


def test_hook_same_failure_persisted_late_repair():
    st = _state(
        band=IterationBand.LATE_REPAIR,
        current_iter=85,
        edited_source_files=["src/widget.py"],
        repeated_failure_count=3,
    )
    msg = hooks.hook_same_failure_persisted(st, _failure())
    _assert_diagnostic(msg, "same_failure_persisted[late_repair]")
    _assert_has_header(msg, "same_failure_persisted[late_repair]")


def test_hook_symptom_convergence():
    st = _state()
    msg = hooks.hook_symptom_convergence(st, "src/render/", "src/bridge.py")
    _assert_diagnostic(msg, "symptom_convergence")
    _assert_has_header(msg, "symptom_convergence")


def test_hook_unverified_patch():
    st = _state(
        edited_source_files=["src/widget.py"],
        last_edit_iter=40,
        broad_pass_after_edit_count=1,
    )
    msg = hooks.hook_unverified_patch(
        st, test_file_suggestions=["tests/test_widget.py"],
    )
    _assert_diagnostic(msg, "unverified_patch")
    _assert_has_header(msg, "unverified_patch")


def test_hook_unsafe_finish_branch_a_unresolved():
    # Branch A: unresolved verification failure.
    st = _state(
        edited_source_files=["src/widget.py"],
        failure_records=[{"failing_unit": "test_widget::test_render"}],
        last_failing_verification_iter=45,
        last_passing_verification_iter=30,
    )
    msg = hooks.hook_unsafe_finish(st, l3_repair_line="guard the None branch")
    _assert_diagnostic(msg, "unsafe_finish[A]")
    _assert_has_header(msg, "unsafe_finish[A]")


def test_hook_unsafe_finish_branch_b_unverified():
    # Branch B: broad tests passed, no targeted verification.
    st = _state(
        edited_source_files=["src/widget.py"],
        last_edit_iter=40,
        broad_pass_after_edit_count=1,
    )
    msg = hooks.hook_unsafe_finish(st)
    _assert_diagnostic(msg, "unsafe_finish[B]")
    _assert_has_header(msg, "unsafe_finish[B]")


def test_hook_unsafe_finish_branch_c_no_verification():
    # Branch C: no verification at all.
    st = _state(
        edited_source_files=["src/widget.py"],
        verification_commands_run=0,
    )
    msg = hooks.hook_unsafe_finish(st)
    _assert_diagnostic(msg, "unsafe_finish[C]")
    _assert_has_header(msg, "unsafe_finish[C]")


# --------------------------------------------------------------------------- #
# governor.py — the two prescriptive advisory builders.
# --------------------------------------------------------------------------- #


class _Action:
    """Minimal stand-in for an OpenHands CmdRunAction (non-edit)."""

    def __init__(self) -> None:
        self.command = "ls -la"


def test_governor_no_source_edits_advisory(tmp_path):
    # Drive the early scaffold-trap "No Source Edits" advisory: many actions,
    # zero source edits, past 20% of budget.
    state_path = tmp_path / "state.json"
    inst = f"gov-nosrc-{os.getpid()}"
    gov = L5Governor(inst, max_iter=100)
    # ensure no leftover disk state interferes
    gov.state.edited_source_files = []
    decision = gov.after_interaction(
        _Action(), obs=None, action_count=30, max_iter=100,
        brief_candidates={"src/a.py", "src/b.py"},
    )
    assert decision.fired, "scaffold trap should fire at 30 actions / 0 edits"
    assert decision.message, "scaffold trap should produce a message"
    _assert_diagnostic(decision.message, "governor:no_source_edits")
    _assert_has_header(decision.message, "governor:no_source_edits")
    # cleanup sidecar
    try:
        os.remove(f"/tmp/gt_l5_state_{inst}.json")
    except OSError:
        pass


def _build_graph_db(path: str) -> None:
    """Minimal graph.db: an edited file with a cross-file caller (conf 0.9)."""
    conn = sqlite3.connect(path)
    conn.executescript(
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
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    # target function lives in the EDITED file
    conn.execute(
        "INSERT INTO nodes(id,label,name,file_path,is_test,language) "
        "VALUES (1,'Function','render','src/widget.py',0,'python')"
    )
    # caller lives in a DIFFERENT, non-test file
    conn.execute(
        "INSERT INTO nodes(id,label,name,file_path,is_test,language) "
        "VALUES (2,'Function','use_widget','src/consumer.py',0,'python')"
    )
    conn.execute(
        "INSERT INTO edges(id,source_id,target_id,type,confidence) "
        "VALUES (1,2,1,'CALLS',0.9)"
    )
    conn.commit()
    conn.close()


def test_governor_scope_check_advisory(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    _build_graph_db(str(db))
    monkeypatch.setenv("GT_GRAPH_DB", str(db))

    inst = f"gov-scope-{os.getpid()}"
    gov = L5Governor(inst, max_iter=100)
    gov.state.record_source_edit("src/widget.py")

    scope_msg = gov._check_multi_file_scope()
    assert scope_msg, "scope check should fire when caller in unedited file exists"
    _assert_diagnostic(scope_msg, "governor:scope_check")
    _assert_has_header(scope_msg, "governor:scope_check")
    try:
        os.remove(f"/tmp/gt_l5_state_{inst}.json")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# NEGATIVE CONTROL (no over-suppression at the verb-list level):
# the goku hooks were ALREADY converted to diagnostic in a prior commit and
# must continue to pass — proving the check is not over-broad and that valid
# diagnostic content is left untouched.
# --------------------------------------------------------------------------- #

def test_already_diagnostic_goku_hooks_unchanged():
    st = _state(
        band=IterationBand.LATE_REPAIR,
        current_iter=85,
        edited_source_files=["src/widget.py"],
        latest_gt_next_action_type="READ_CALLER_CONTRACT",
        actions_since_gt_next_action=4,
    )
    for builder, args, label in [
        (hooks.hook_structural_witness_ignored, (st, "src/consumer.py"), "goku:witness_ignored"),
        (hooks.hook_patch_collapsed_or_lost, (_state(patch_collapsed=True, band=IterationBand.LATE_REPAIR, current_iter=85),), "goku:patch_collapsed"),
        (hooks.hook_no_durable_progress_goku, (_state(band=IterationBand.FINALIZATION, current_iter=95, edited_source_files=[]),), "goku:no_durable_progress"),
    ]:
        msg = builder(*args)
        _assert_diagnostic(msg, label)
        _assert_has_header(msg, label)

    # weak-verification-after-edit
    wv = _state(
        edited_source_files=["src/widget.py"],
        last_edit_iter=40,
        last_passing_targeted_iter=0,
        broad_pass_after_edit_count=1,
    )
    msg = hooks.hook_weak_verification_after_edit(wv)
    _assert_diagnostic(msg, "goku:weak_verification")
    _assert_has_header(msg, "goku:weak_verification")

    # finish-without-structural-witness
    fw = _state(
        edited_source_files=["src/widget.py"],
        last_edit_iter=40,
        last_passing_targeted_iter=0,
        structural_witness_followed=False,
    )
    msg = hooks.hook_finish_without_structural_witness(fw)
    _assert_diagnostic(msg, "goku:finish_without_witness")
    _assert_has_header(msg, "goku:finish_without_witness")
