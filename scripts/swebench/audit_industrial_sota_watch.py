#!/usr/bin/env python3
"""Audit whether the industrial/SOTA watch loop is working as intended."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WATCH = ROOT / ".groundtruth" / "industrial_sota_watch.jsonl"
DEFAULT_PID = ROOT / ".groundtruth" / "industrial_sota_watch.pid"
REQUIRED_LANGS = {"go", "javascript", "python", "rust", "typescript"}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            rows.append(
                {
                    "schema": "gt.industrial_sota_watch_snapshot.invalid",
                    "line": n,
                    "error": str(exc),
                }
            )
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _pid_running(pid: int | None) -> bool | None:
    if pid is None:
        return None
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return f'"{pid}"' in proc.stdout or f",{pid}," in proc.stdout
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_from_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _finding(item: str, status: str, evidence: list[str], risk: str = "") -> dict[str, Any]:
    return {
        "item": item,
        "status": status,
        "evidence": evidence,
        "risk": risk,
    }


def _read(snapshot: dict[str, Any]) -> dict[str, Any]:
    data = snapshot.get("trajectory_read")
    return data if isinstance(data, dict) else {}


def audit(watch_path: Path, pid_file: Path, *, max_age_sec: int) -> dict[str, Any]:
    rows = _load_jsonl(watch_path)
    valid = [r for r in rows if r.get("schema") == "gt.industrial_sota_watch_snapshot.v1"]
    invalid = [r for r in rows if r.get("schema") == "gt.industrial_sota_watch_snapshot.invalid"]
    latest = valid[-1] if valid else {}
    read = _read(latest)
    run = latest.get("trajectory_run") if isinstance(latest.get("trajectory_run"), dict) else {}
    pid_file_value = _pid_from_file(pid_file)
    latest_pid = latest.get("pid") if isinstance(latest.get("pid"), int) else None
    latest_age = time.time() - float(latest.get("ts_epoch") or 0) if latest else None
    pid_running = _pid_running(pid_file_value)

    findings: list[dict[str, Any]] = []
    findings.append(
        _finding(
            "watch_log_parseable",
            "pass" if rows and not invalid else "fail",
            [
                f"rows={len(rows)}",
                f"valid_snapshots={len(valid)}",
                f"invalid_rows={len(invalid)}",
            ],
            "Cannot trust counters if JSONL snapshots are corrupt." if invalid or not rows else "",
        )
    )
    findings.append(
        _finding(
            "active_pid",
            "pass" if pid_file_value and latest_pid == pid_file_value and pid_running else "fail",
            [
                f"pid_file={pid_file_value}",
                f"latest_snapshot_pid={latest_pid}",
                f"pid_running={pid_running}",
            ],
            "Watcher may be stale or stopped." if not (pid_file_value and latest_pid == pid_file_value and pid_running) else "",
        )
    )
    findings.append(
        _finding(
            "fresh_snapshot",
            "pass" if latest_age is not None and latest_age <= max_age_sec else "fail",
            [f"latest_age_sec={round(latest_age, 1) if latest_age is not None else None}", f"max_age_sec={max_age_sec}"],
            "Watcher is not appending fresh evidence." if latest_age is None or latest_age > max_age_sec else "",
        )
    )
    findings.append(
        _finding(
            "trajectory_run_clean",
            "pass" if run.get("rc") == 0 else "fail",
            [f"trajectory_run_rc={run.get('rc')}", f"duration_s={run.get('duration_s')}"],
            "The watcher ran the trajectory but the trajectory command failed." if run.get("rc") != 0 else "",
        )
    )
    summary = read.get("summary_record") or {}
    findings.append(
        _finding(
            "trajectory_checks_green",
            "pass" if summary.get("step_count") == summary.get("ok_count") and summary.get("failed_count") == 0 else "fail",
            [
                f"step_count={summary.get('step_count')}",
                f"ok_count={summary.get('ok_count')}",
                f"failed_count={summary.get('failed_count')}",
            ],
            "Local validation trajectory has failing checks." if summary.get("failed_count") else "",
        )
    )
    ledger = read.get("ledger") or {}
    findings.append(
        _finding(
            "ledger_not_closed_by_local_hygiene",
            "pass" if ledger.get("remaining") == 27 and (ledger.get("statuses") or {}).get("in_progress") == 27 else "fail",
            [f"ledger={ledger}"],
            "Local checks must not close TODOs; only targeted remote/containerized evidence can." if ledger.get("remaining") != 27 else "",
        )
    )
    gate = read.get("industrial_gate_counts") or {}
    findings.append(
        _finding(
            "old_artifacts_do_not_close_todos",
            "pass" if gate.get("missing", 0) > 0 else "fail",
            [f"industrial_gate_counts={gate}"],
            "Old smoke artifacts should not be enough to close the ledger." if gate.get("missing", 0) == 0 else "",
        )
    )
    iteration_langs = set((read.get("iteration_surface_languages") or {}).keys())
    deepswe_langs = set((read.get("deepswe_surface_languages") or {}).keys())
    findings.append(
        _finding(
            "all_five_languages_iteration",
            "pass" if iteration_langs == REQUIRED_LANGS else "fail",
            [f"iteration_languages={sorted(iteration_langs)}", f"required={sorted(REQUIRED_LANGS)}"],
            "Iteration surface is not proving all supported languages." if iteration_langs != REQUIRED_LANGS else "",
        )
    )
    findings.append(
        _finding(
            "all_five_languages_deepswe_explicit",
            "pass" if deepswe_langs == REQUIRED_LANGS else "fail",
            [f"deepswe_languages={sorted(deepswe_langs)}", f"required={sorted(REQUIRED_LANGS)}"],
            "Held-out selector is not covering all supported languages." if deepswe_langs != REQUIRED_LANGS else "",
        )
    )
    resolution = read.get("resolution_audit") or {}
    findings.append(
        _finding(
            "resolution_audit_has_risk_counters",
            "pass"
            if all(k in resolution for k in ("call_edges", "untrusted_name_match_call_edges", "ambiguous_candidate_call_edges", "trusted_call_edges"))
            else "fail",
            [f"resolution_audit={resolution}"],
            "G2 audit is not surfacing risk counters." if not resolution else "",
        )
    )

    status_counts: dict[str, int] = {}
    for finding in findings:
        status = str(finding["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "schema": "gt.industrial_sota_watch_audit.v1",
        "watch_path": str(watch_path),
        "pid_file": str(pid_file),
        "status_counts": status_counts,
        "findings": findings,
        "latest_snapshot_iteration": latest.get("iteration"),
        "latest_snapshot_pid": latest_pid,
        "latest_snapshot_age_sec": round(latest_age, 1) if latest_age is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", default=str(DEFAULT_WATCH))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID))
    parser.add_argument("--max-age-sec", type=int, default=420)
    args = parser.parse_args(argv)
    payload = audit(Path(args.watch), Path(args.pid_file), max_age_sec=args.max_age_sec)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status_counts"].get("fail", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
