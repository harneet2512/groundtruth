"""P0-06 — gt_deep_metrics prefers task_truth.json for outcome authority."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile

_DM_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "swebench", "gt_deep_metrics.py"
)
_spec = importlib.util.spec_from_file_location("gt_deep_metrics_tt", _DM_PATH)
dm = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(dm)


def test_build_prefers_task_truth_failure_class():
    with tempfile.TemporaryDirectory() as td:
        truth = {
            "outcome": {
                "failure_class": "INFRA",
                "resolved": False,
                "in_resolved_denominator": False,
            },
            "runtime_control": {
                "phase_policy_version": "gt.runtime.context_policy.v1",
                "enforcement_semantics": {"hard_enforced": False},
            },
        }
        with open(os.path.join(td, "task_truth.json"), "w", encoding="utf-8") as fh:
            json.dump(truth, fh)
        # Minimal trajectory so build() does not early-exit.
        traj_dir = os.path.join(td, "jobs", "run", "task__x", "agent")
        os.makedirs(traj_dir, exist_ok=True)
        with open(
            os.path.join(traj_dir, "mini-swe-agent.trajectory.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump({"messages": [], "info": {"model_stats": {}}}, fh)
        payload = dm.build("task", td)
        assert payload["failure_class"] == "INFRA"
        assert payload["outcome_authority"] == "task_truth.json"
        assert payload["resolved"] is False
        assert payload["runtime_control"]["phase_policy_version"] == "gt.runtime.context_policy.v1"
