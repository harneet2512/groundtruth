"""Drift deep-metrics emitter (scripts/drift/deep_metrics.compute_drift_metrics).

Synthetic OH history proves: drift blocks are extracted from agent observations (raw
text, AGENT-OBSERVATION rule), utilization scores the agent's post-drift reaction,
agent action/edit stats match the compute_run_metrics schema, and flip/regression are
computed vs the frozen baseline.
"""
from __future__ import annotations

import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "drift_deep_metrics",
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "drift", "deep_metrics.py"),
)
dm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dm)

_DRIFT = (
    "<gt-drift>\nYour edit changed the behavioral contract below.\n"
    "lib.py :: get_user  (1 verified caller depend on this)\n"
    "  return shape: list -> none\n  dropped raise: KeyError\n</gt-drift>"
)


def _history_reacted():
    return [
        {"action": "read", "args": {"path": "lib.py"}},
        {"action": "edit", "args": {"path": "lib.py", "str_replace": "..."}},
        {"observation": "run", "content": _DRIFT},          # agent sees drift
        {"action": "message", "content": "I should restore the KeyError in get_user"},  # engaged (symbol)
        {"action": "edit", "args": {"path": "lib.py", "str_replace": "fix"}},  # reacted (edit after)
    ]


def test_drift_emitted_and_full_utilization():
    rec = dm.compute_drift_metrics(_history_reacted(), resolved="RESOLVED", patch="diff --git",
                                   task="beets-1", baseline_ids={"other-task"})
    assert rec["drift"]["emitted"] == 1
    assert rec["drift"]["raw_blocks"] and "dropped raise: KeyError" in rec["drift"]["raw_blocks"][0]
    assert "get_user" in rec["drift"]["named_symbols"]
    # edited after drift (+0.5) AND named symbol referenced after (+0.5) = 1.0
    assert rec["drift"]["utilization_score"] == 1.0
    assert rec["agent"]["edit_count"] == 2
    # actions are [read, edit, edit]; the first edit is the 2nd action.
    assert rec["agent"]["first_edit_action"] == 2
    assert "lib.py" in rec["agent"]["edited_files"]
    # resolved + not in baseline => flip
    assert rec["outcome"]["flip"] is True
    assert rec["outcome"]["regression"] is False


def test_drift_ignored_zero_utilization():
    hist = [
        {"action": "edit", "args": {"path": "lib.py"}},
        {"observation": "run", "content": _DRIFT},  # drift is the LAST event; no reaction after
    ]
    rec = dm.compute_drift_metrics(hist, resolved="NO", patch="", task="t", baseline_ids=set())
    assert rec["drift"]["emitted"] == 1
    assert rec["drift"]["utilization_score"] == 0.0  # no edit/reference after drift


def test_no_drift_quiet():
    hist = [{"action": "edit", "args": {"path": "lib.py"}}, {"observation": "run", "content": "ok"}]
    rec = dm.compute_drift_metrics(hist, resolved="NO", patch="", task="t", baseline_ids=set())
    assert rec["drift"]["emitted"] == 0
    assert rec["drift"]["utilization_score"] == 0.0
    assert rec["drift"]["raw_blocks"] == []


def test_regression_flag_vs_baseline():
    hist = [{"action": "edit", "args": {"path": "lib.py"}}]
    rec = dm.compute_drift_metrics(hist, resolved="NO", patch="", task="was-passing",
                                   baseline_ids={"was-passing"})
    assert rec["outcome"]["regression"] is True
    assert rec["outcome"]["flip"] is False
