"""Pins truthful, task-scoped progress logging for paid matrix runs."""
from __future__ import annotations

from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "swebench_live_lite_full.yml"
)


def _trial_run() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["trial"]["steps"]:
        if isinstance(step, dict) and step.get("name") == "Run GT Pro trial":
            return str(step.get("run") or "")
    raise AssertionError("paid trial run block not found")


def test_live_heartbeat_identifies_task_and_observable_progress() -> None:
    run = _trial_run()
    assert "[GT_HEARTBEAT]" in run
    for field in (
        "task=$MTASK",
        "phase=trial",
        "ledger_rows=",
        "oracle_rows=",
        "trajectory_bytes=",
        "containers=",
    ):
        assert field in run, f"heartbeat lacks {field}"
    assert "sleep 8" in run
    assert "gt_heartbeat.log" in run
    heartbeat_line = next(line for line in run.splitlines() if "[GT_HEARTBEAT]" in line)
    assert "trial_output.log" not in heartbeat_line


def test_failure_log_and_partial_artifacts_are_always_uploaded() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["trial"]["steps"]
    collect = next(step for step in steps if step.get("name") == "Collect results")
    upload = next(step for step in steps if step.get("name") == "Upload results")
    progress = next(
        step for step in steps if step.get("name") == "Upload post-agent diagnostic snapshot"
    )
    assert collect.get("if") == "always()"
    assert upload.get("if") == "always()"
    assert progress.get("if") == "always()"
    assert "trial_output.log" in collect.get("run", "")
    assert progress["with"]["name"] == "ll-progress-${{ matrix.task }}"
    assert "/tmp/gt_out/gt_agent_exit.json" in progress["with"]["path"]
    assert "gt_heartbeat.log" in progress["with"]["path"]
    assert progress["with"]["if-no-files-found"] == "error"
    assert upload["with"]["if-no-files-found"] == "error"
