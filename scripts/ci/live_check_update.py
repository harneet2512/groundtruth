#!/usr/bin/env python3
"""Publish payload-free per-task progress through the GitHub Checks API.

The Actions UI may hide a running matrix job's log.  This bridge exposes only
operational counters already written by ``GT_HEARTBEAT``; it never reads or
publishes trajectory content, issue text, model output, test identities, or
environment values.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import time
from typing import Any
from urllib import request


_STOP = False


def _stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _last_heartbeat(path: Path) -> str:
    """Return only allowlisted counters from the newest heartbeat line."""
    if not path.is_file():
        return "waiting"
    latest: str | None = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("[GT_HEARTBEAT]"):
                latest = line.strip()
    if latest is None:
        return "waiting"
    fields: list[str] = []
    fixed_patterns = (
        ("phase", r"\bphase=([a-z_]{1,32})\b"),
        ("updated", r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b"),
        ("ledger_rows", r"\bledger_rows=(\d{1,20})\b"),
        ("oracle_rows", r"\boracle_rows=(\d{1,20})\b"),
        ("trajectory_bytes", r"\btrajectory_bytes=(\d{1,20})\b"),
        ("progress_age_s", r"\bprogress_age_s=(\d{1,20})\b"),
        (
            "container_mem",
            r"\bcontainer_mem=(\d+(?:\.\d+)?[KMGT]?i?B/\d+(?:\.\d+)?[KMGT]?i?B)\b",
        ),
        ("container_pids", r"\bcontainer_pids=(\d{1,20})\b"),
        ("container_top_rss_kb", r"\bcontainer_top_rss_kb=(\d{1,20})\b"),
        ("container_oom", r"\bcontainer_oom=([01])\b"),
        (
            "container_mem_current_bytes",
            r"\bcontainer_mem_current_bytes=(\d{1,20})\b",
        ),
        (
            "container_mem_peak_bytes",
            r"\bcontainer_mem_peak_bytes=(\d{1,20})\b",
        ),
        (
            "container_oom_kill_count",
            r"\bcontainer_oom_kill_count=(\d{1,20})\b",
        ),
        ("swap_used_mb", r"\bswap_used_mb=(\d{1,20})\b"),
        ("mem", r"\bmem=(\d{1,20}/\d{1,20}MB)\b"),
        ("disk_used", r"\bdisk_used=(\d{1,20}/\d{1,20}MB)\b"),
        ("disk_free", r"\bfree=(\d{1,20}MB)\b"),
        ("containers", r"\bcontainers=(\d{1,10})\b"),
    )
    for key, pattern in fixed_patterns:
        match = re.search(pattern, latest)
        if match:
            fields.append(f"{key}={match.group(1)}")
    return " ".join(fields) if fields else "waiting"


def build_summary(
    *,
    task: str,
    heartbeat_path: Path,
    trial_log_path: Path,
    trajectory_path: Path,
    agent_exit_path: Path,
    run_id: str,
    head_sha: str,
    now_utc: str,
) -> tuple[str, str]:
    """Return a safe phase/title and Markdown summary from metadata only."""
    trajectory_bytes = (
        trajectory_path.stat().st_size if trajectory_path.is_file() else 0
    )
    trial_log_bytes = (
        trial_log_path.stat().st_size if trial_log_path.is_file() else 0
    )
    if agent_exit_path.is_file():
        phase = "agent_complete"
    elif trajectory_bytes > 0:
        phase = "agent_running"
    else:
        phase = "trial_start"
    heartbeat = _last_heartbeat(heartbeat_path)
    title = f"{task}: {phase}"
    summary = "\n".join((
        f"- task: `{task}`",
        f"- phase: `{phase}`",
        f"- updated_utc: `{now_utc}`",
        f"- workflow_run_id: `{run_id}`",
        f"- head_sha: `{head_sha}`",
        f"- trial_log_bytes: `{trial_log_bytes}`",
        f"- trajectory_bytes: `{trajectory_bytes}`",
        f"- latest_heartbeat: `{heartbeat}`",
        "",
        "Payload-free telemetry only; this check is not delivery or SS-LIVE proof.",
    ))
    return title, summary


class CheckClient:
    def __init__(self, *, repository: str, token: str, head_sha: str) -> None:
        self.repository = repository
        self.token = token
        self.head_sha = head_sha
        self.base = f"https://api.github.com/repos/{repository}/check-runs"

    def _request(self, url: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "groundtruth-live-check",
            },
        )
        with request.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed GitHub API
            body = response.read().decode("utf-8")
        loaded = json.loads(body)
        return loaded if isinstance(loaded, dict) else {}

    def create(self, *, name: str, title: str, summary: str) -> int:
        result = self._request(self.base, "POST", {
            "name": name,
            "head_sha": self.head_sha,
            "status": "in_progress",
            "output": {"title": title, "summary": summary},
        })
        check_id = result.get("id")
        if not isinstance(check_id, int) or isinstance(check_id, bool):
            raise ValueError("GitHub Checks create response had no integer id")
        return check_id

    def update(
        self,
        check_id: int,
        *,
        title: str,
        summary: str,
        completed: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "status": "completed" if completed else "in_progress",
            "output": {"title": title, "summary": summary},
        }
        if completed:
            payload["conclusion"] = "neutral"
        self._request(f"{self.base}/{check_id}", "PATCH", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task")
    parser.add_argument("--heartbeat", default="gt_heartbeat.log")
    parser.add_argument("--trial-log", default="trial_output.log")
    parser.add_argument("--trajectory", default="/tmp/gt_out/mini-swe-agent.trajectory.json")
    parser.add_argument("--agent-exit", default="/tmp/gt_out/gt_agent_exit.json")
    parser.add_argument("--interval", type=float, default=120.0)
    args = parser.parse_args(argv)

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GT_LIVE_CHECK_TOKEN", "")
    head_sha = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not repository or not token or not head_sha or not run_id:
        print("[gt-live-check] disabled: workflow identity/token missing", flush=True)
        return 0
    if args.interval < 120:
        raise ValueError("live-check interval must be at least 120 seconds")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    client = CheckClient(repository=repository, token=token, head_sha=head_sha)
    check_id: int | None = None
    paths = {
        "heartbeat_path": Path(args.heartbeat),
        "trial_log_path": Path(args.trial_log),
        "trajectory_path": Path(args.trajectory),
        "agent_exit_path": Path(args.agent_exit),
    }
    name = f"gt-live/{args.task}"[:100]
    while True:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        title, summary = build_summary(
            task=args.task,
            run_id=run_id,
            head_sha=head_sha,
            now_utc=now,
            **paths,
        )
        try:
            if check_id is None:
                check_id = client.create(name=name, title=title, summary=summary)
            else:
                client.update(check_id, title=title, summary=summary)
        except Exception as exc:
            print(f"[gt-live-check] update failed: {type(exc).__name__}", flush=True)
        if _STOP:
            if check_id is not None:
                try:
                    client.update(
                        check_id,
                        title=f"{args.task}: trial step complete",
                        summary=summary,
                        completed=True,
                    )
                except Exception as exc:
                    print(
                        f"[gt-live-check] completion failed: {type(exc).__name__}",
                        flush=True,
                    )
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
