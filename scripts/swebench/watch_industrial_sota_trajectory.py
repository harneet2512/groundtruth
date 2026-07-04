#!/usr/bin/env python3
"""Continuously run and read the industrial/SOTA local validation trajectory."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from table_industrial_sota_audit import _markdown, build_table
from read_industrial_sota_trajectory import summarize


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRAJECTORY = ROOT / ".groundtruth" / "industrial_sota_local_trajectory.jsonl"
DEFAULT_SNAPSHOTS = ROOT / ".groundtruth" / "industrial_sota_watch.jsonl"
DEFAULT_PID = ROOT / ".groundtruth" / "industrial_sota_watch.pid"


def _run_trajectory(trajectory: Path) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "swebench" / "run_industrial_sota_local_trajectory.py"),
        "--out",
        str(trajectory),
    ]
    start = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "cmd": cmd,
        "rc": proc.returncode,
        "duration_s": round(time.time() - start, 3),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def _snapshot(*, trajectory: Path, run_result: dict[str, Any], iteration: int) -> dict[str, Any]:
    try:
        read = summarize(trajectory)
    except Exception as exc:
        read = {
            "schema": "gt.local_validation_trajectory_read.v1",
            "read_error": f"{type(exc).__name__}: {exc}",
        }
    try:
        table = build_table(trajectory, ROOT / ".groundtruth" / "industrial_sota_ledger.json")
        table_markdown = _markdown(table)
    except Exception as exc:
        table = {
            "schema": "gt.industrial_sota_27_table.v1",
            "read_error": f"{type(exc).__name__}: {exc}",
        }
        table_markdown = f"TABLE_READ_ERROR: {type(exc).__name__}: {exc}"
    return {
        "schema": "gt.industrial_sota_watch_snapshot.v1",
        "pid": os.getpid(),
        "iteration": iteration,
        "ts_epoch": time.time(),
        "trajectory_run": run_result,
        "trajectory_read": read,
        "trajectory_table": table,
        "trajectory_table_markdown": table_markdown,
    }


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-sec", type=int, default=300)
    parser.add_argument("--iterations", type=int, default=0, help="0 means run forever")
    parser.add_argument("--trajectory", default=str(DEFAULT_TRAJECTORY))
    parser.add_argument("--snapshots", default=str(DEFAULT_SNAPSHOTS))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID))
    args = parser.parse_args(argv)

    trajectory = Path(args.trajectory)
    snapshots = Path(args.snapshots)
    pid_file = Path(args.pid_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    iteration = 0
    while True:
        iteration += 1
        run_result = _run_trajectory(trajectory)
        snap = _snapshot(trajectory=trajectory, run_result=run_result, iteration=iteration)
        _append_jsonl(snapshots, snap)
        print(json.dumps(snap["trajectory_read"], sort_keys=True), flush=True)
        print(snap["trajectory_table_markdown"], flush=True)
        if args.iterations and iteration >= args.iterations:
            return 0 if run_result["rc"] == 0 else run_result["rc"]
        time.sleep(max(1, args.interval_sec))


if __name__ == "__main__":
    raise SystemExit(main())
