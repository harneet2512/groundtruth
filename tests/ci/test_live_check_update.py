from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).parents[2] / "scripts" / "ci" / "live_check_update.py"
_SPEC = importlib.util.spec_from_file_location("live_check_update", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
live = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live)


def test_summary_is_payload_free_and_reports_only_operational_metadata(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.log"
    heartbeat.write_text(
        "[GT_HEARTBEAT] task=repo__task phase=trial ledger_rows=3 "
        "oracle_rows=1 trajectory_bytes=420 mem=100/200MB "
        "progress_age_s=8 container_mem=12.5GiB/12.7GiB container_pids=42 "
        "container_top_rss_kb=123456 container_oom=0 swap_used_mb=7 "
        "container_mem_current_bytes=1024 container_mem_peak_bytes=2048 "
        "container_oom_kill_count=0 "
        "SECRET_ASSERTION=do-not-publish\n"
        "ignored model payload SECRET_ASSERTION\n",
        encoding="utf-8",
    )
    trial = tmp_path / "trial.log"
    trial.write_text(
        "FAIL_TO_PASS test_private_name SECRET_ASSERTION\n",
        encoding="utf-8",
    )
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_bytes(b"private trajectory bytes")

    title, summary = live.build_summary(
        task="repo__task",
        heartbeat_path=heartbeat,
        trial_log_path=trial,
        trajectory_path=trajectory,
        agent_exit_path=tmp_path / "missing_exit.json",
        run_id="123",
        head_sha="a" * 40,
        now_utc="2026-07-15T12:00:00Z",
    )

    assert title == "repo__task: agent_running"
    assert "ledger_rows=3" in summary
    assert "progress_age_s=8" in summary
    assert "container_mem=12.5GiB/12.7GiB" in summary
    assert "container_pids=42" in summary
    assert "container_top_rss_kb=123456" in summary
    assert "container_oom=0" in summary
    assert "container_mem_current_bytes=1024" in summary
    assert "container_mem_peak_bytes=2048" in summary
    assert "container_oom_kill_count=0" in summary
    assert "swap_used_mb=7" in summary
    assert "trajectory_bytes: `24`" in summary
    assert "trial_log_bytes:" in summary
    assert "SECRET_ASSERTION" not in summary
    assert "FAIL_TO_PASS" not in summary
    assert "private trajectory bytes" not in summary
    assert "SECRET_ASSERTION=do-not-publish" not in summary


def test_summary_moves_to_agent_complete_from_receipt_presence(tmp_path: Path) -> None:
    agent_exit = tmp_path / "gt_agent_exit.json"
    agent_exit.write_text("{}", encoding="utf-8")

    title, summary = live.build_summary(
        task="repo__task",
        heartbeat_path=tmp_path / "missing.log",
        trial_log_path=tmp_path / "missing-trial.log",
        trajectory_path=tmp_path / "missing-trajectory.json",
        agent_exit_path=agent_exit,
        run_id="456",
        head_sha="b" * 40,
        now_utc="2026-07-15T12:01:00Z",
    )

    assert title == "repo__task: agent_complete"
    assert "latest_heartbeat: `waiting`" in summary


def test_check_client_never_embeds_token_in_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(self, url, method, payload):
        captured.update(url=url, method=method, payload=payload)
        return {"id": 17}

    monkeypatch.setattr(live.CheckClient, "_request", fake_request)
    client = live.CheckClient(
        repository="owner/repo", token="do-not-publish", head_sha="c" * 40,
    )

    assert client.create(name="gt-live/task", title="task", summary="safe") == 17
    assert captured["method"] == "POST"
    assert "do-not-publish" not in repr(captured["payload"])


def test_completed_update_has_terminal_check_contract(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(self, url, method, payload):
        captured.update(url=url, method=method, payload=payload)
        return {}

    monkeypatch.setattr(live.CheckClient, "_request", fake_request)
    client = live.CheckClient(
        repository="owner/repo", token="token", head_sha="d" * 40,
    )
    client.update(17, title="done", summary="safe", completed=True)

    assert captured["method"] == "PATCH"
    assert str(captured["url"]).endswith("/check-runs/17")
    assert captured["payload"] == {
        "status": "completed",
        "conclusion": "neutral",
        "output": {"title": "done", "summary": "safe"},
    }


def test_paid_workflow_wires_checks_without_content_write_permission() -> None:
    workflow = (
        Path(__file__).parents[2]
        / ".github" / "workflows" / "swebench_live_lite_full.yml"
    ).read_text(encoding="utf-8")

    assert "permissions:\n  contents: read\n  packages: read\n  checks: write" in workflow
    assert "GT_LIVE_CHECK_TOKEN: ${{ github.token }}" in workflow
    assert 'python3 scripts/ci/live_check_update.py "$MTASK" --interval 120 &' in workflow
    assert 'kill "$_RESMON_PID" "$_GT_LIVE_CHECK_PID"' in workflow
    assert "contents: write" not in workflow
