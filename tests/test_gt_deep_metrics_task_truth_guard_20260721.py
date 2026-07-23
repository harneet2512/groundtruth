"""Codex root-cause #1 — gt_deep_metrics must honor task_truth.json ONLY when it is
current-schema AND identity-matched to the task under grade.

The frozen-baseline task_truth copies carry ``instance_id: null`` and
``failure_class: INFRA``. The pre-guard emitter honored ANY task_truth found via a
recursive glob, so those stale copies overrode reward/trajectory truth and forced
every one of the frozen 83 resolves to unresolved. These tests pin the guard:

* stale (null instance_id) truth is IGNORED;
* foreign (mismatched instance_id) truth is IGNORED;
* valid, identity-matched truth is HONORED (P0-06 preserved);
* the ``ll-full-`` task-dir prefix is normalized on both sides.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile

_DM_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "swebench", "gt_deep_metrics.py"
)
_spec = importlib.util.spec_from_file_location("gt_deep_metrics_ttguard", _DM_PATH)
dm = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(dm)


def _run(td: str, truth: dict | None, task: str = "task") -> dict:
    """Write an optional task_truth + a minimal trajectory, then grade the task."""
    if truth is not None:
        with open(os.path.join(td, "task_truth.json"), "w", encoding="utf-8") as fh:
            json.dump(truth, fh)
    traj_dir = os.path.join(td, "jobs", "run", "task__x", "agent")
    os.makedirs(traj_dir, exist_ok=True)
    with open(
        os.path.join(traj_dir, "mini-swe-agent.trajectory.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump({"messages": [], "info": {"model_stats": {}}}, fh)
    return dm.build(task, td)


def _infra_truth(**identity) -> dict:
    truth = {
        "outcome": {
            "failure_class": "INFRA",
            "resolved": False,
            "in_resolved_denominator": False,
        },
        "runtime_control": {"phase_policy_version": "gt.runtime.context_policy.v1"},
    }
    truth.update(identity)
    return truth


def test_null_instance_id_truth_is_ignored():
    """The exact frozen-baseline shape: schema-less/null-iid INFRA truth must NOT win."""
    with tempfile.TemporaryDirectory() as td:
        payload = _run(td, _infra_truth(schema="gt.task_truth.v1", instance_id=None))
    assert payload["outcome_authority"] != "task_truth.json"
    assert payload["failure_class"] != "INFRA"
    assert payload["task_truth_rejected"] is not None
    assert payload["task_truth_rejected"]["reason"] == "schema_or_instance_id_not_matched_to_task"


def test_foreign_instance_id_truth_is_ignored():
    """A valid-schema truth for a DIFFERENT task must not override this task."""
    with tempfile.TemporaryDirectory() as td:
        payload = _run(
            td, _infra_truth(schema="gt.task_truth.v1", instance_id="some-other-task-99")
        )
    assert payload["outcome_authority"] != "task_truth.json"
    assert payload["task_truth_rejected"] is not None


def test_missing_schema_truth_is_ignored():
    """Identity present but stale schema → ignored (schema is part of the contract)."""
    with tempfile.TemporaryDirectory() as td:
        payload = _run(td, _infra_truth(instance_id="task"))  # no schema key
    assert payload["outcome_authority"] != "task_truth.json"
    assert payload["task_truth_rejected"] is not None


def test_valid_matched_truth_is_honored():
    """P0-06 preserved: a current-schema, identity-matched truth IS authoritative."""
    with tempfile.TemporaryDirectory() as td:
        payload = _run(td, _infra_truth(schema="gt.task_truth.v1", instance_id="task"))
    assert payload["outcome_authority"] == "task_truth.json"
    assert payload["failure_class"] == "INFRA"
    assert payload["resolved"] is False
    assert payload["task_truth_rejected"] is None


def test_ll_full_prefix_is_normalized():
    """The 'll-full-' task-dir prefix must not defeat identity matching."""
    with tempfile.TemporaryDirectory() as td:
        payload = _run(
            td,
            _infra_truth(schema="gt.task_truth.v1", instance_id="mytask"),
            task="ll-full-mytask",
        )
    assert payload["outcome_authority"] == "task_truth.json"
    assert payload["task_truth_rejected"] is None


def test_no_task_truth_is_clean():
    """Absent task_truth: no rejection record, outcome from the run itself."""
    with tempfile.TemporaryDirectory() as td:
        payload = _run(td, None)
    assert payload["outcome_authority"] != "task_truth.json"
    assert payload["task_truth_rejected"] is None
