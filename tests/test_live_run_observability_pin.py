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
        "progress_age_s=",
        "container_mem=",
        "container_pids=",
        "container_top_rss_kb=",
        "container_oom=",
        "container_mem_current_bytes=",
        "container_mem_peak_bytes=",
        "container_oom_kill_count=",
        "swap_used_mb=",
        "containers=",
    ):
        assert field in run, f"heartbeat lacks {field}"
    assert "sleep 8" in run
    assert "gt_heartbeat.log" in run
    heartbeat_line = next(line for line in run.splitlines() if "[GT_HEARTBEAT]" in line)
    assert "trial_output.log" not in heartbeat_line


def test_paid_agent_is_resource_bounded_with_upload_headroom() -> None:
    """A runaway task container must die before it can kill the artifact uploader."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    trial = doc["jobs"]["trial"]
    run = _trial_run()
    steps = trial["steps"]

    assert trial["timeout-minutes"] == 150
    assert steps[0]["name"] == "Mark authoritative trial-job start epoch"
    assert 'echo "GT_JOB_STARTED=$(date +%s)" >> "$GITHUB_ENV"' in steps[0]["run"]
    assert sum(
        'GT_JOB_STARTED=$(date +%s)' in str(step.get("run") or "")
        for step in steps
    ) == 1
    assert 'GT_AGENT_WALL_TIMEOUT_S="${GT_AGENT_WALL_TIMEOUT_S:-5400}"' in run
    assert 'GT_RUNNER_HOST_RESERVE_MB="${GT_RUNNER_HOST_RESERVE_MB:-3072}"' in run
    assert 'GT_AGENT_SWAP_ALLOWANCE_MB="${GT_AGENT_SWAP_ALLOWANCE_MB:-2048}"' in run
    assert "GT_AGENT_MIN_UPLOAD_RESERVE_S=1800" in run
    assert "GT_AGENT_RESOURCE_PREFLIGHT_FAIL" in run
    assert '--memory="${GT_AGENT_MEMORY_MB}m"' in run
    assert "GT_AGENT_MEMORY_MB=$((_GT_HOST_MEM_MB - GT_RUNNER_HOST_RESERVE_MB))" in run
    assert "GT_AGENT_MEMORY_SWAP_MB=$((GT_AGENT_MEMORY_MB + GT_AGENT_SWAP_ALLOWANCE_MB))" in run
    assert '--memory-swap="${GT_AGENT_MEMORY_SWAP_MB}m"' in run
    assert (
        'timeout --signal=TERM --kill-after=30s '
        '"${GT_AGENT_WALL_TIMEOUT_EFFECTIVE_S}s"'
    ) in run
    assert 'AGENT_RC=${PIPESTATUS[0]}' in run
    assert "GT_AGENT_WALL_TIMEOUT" in run
    assert 'docker rm -f "gt-pro-$MTASK"' in run
    assert "GT_AGENT_CONTAINER_OOM=$(docker inspect" in run
    assert 'GT_AGENT_TERMINATION="memory_limit"' in run
    assert 'GT_AGENT_TERMINATION="wall_timeout"' in run
    assert '"termination": os.environ["GT_AGENT_TERMINATION"]' in run
    assert '"container_oom_killed": os.environ["GT_AGENT_CONTAINER_OOM"] == "1"' in run


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
