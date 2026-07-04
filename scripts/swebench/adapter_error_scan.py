#!/usr/bin/env python3
"""Structural DeepSWE adapter error scan (P1-11).

Pier can swallow adapter exceptions into per-trial result.json while exiting
rc=0. This module walks jobs/ for result.json files and surfaces adapter
failures from structured fields — not log grep.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any


ADAPTER_MARKERS = ("DeepSweAdapterError", "DEEPSWE_ADAPTER_FAIL")


def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _extract_adapter_error(record: dict[str, Any]) -> str | None:
    """Return a human-readable adapter error string when present."""
    for key in ("exception_message", "error", "message"):
        val = record.get(key)
        if isinstance(val, str) and any(m in val for m in ADAPTER_MARKERS):
            return val.strip()
    info = record.get("info")
    if isinstance(info, dict):
        for key in ("exception_message", "error", "message"):
            val = info.get(key)
            if isinstance(val, str) and any(m in val for m in ADAPTER_MARKERS):
                return val.strip()
    exc = record.get("exception")
    if isinstance(exc, dict):
        typ = str(exc.get("type") or exc.get("name") or "")
        msg = str(exc.get("message") or exc.get("value") or "")
        if any(m in typ or m in msg for m in ADAPTER_MARKERS):
            return f"{typ}: {msg}".strip(": ")
    return None


def scan_jobs_dir(jobs_dir: str) -> list[dict[str, str]]:
    """Walk jobs_dir for result.json adapter failures."""
    hits: list[dict[str, str]] = []
    if not jobs_dir or not os.path.isdir(jobs_dir):
        return hits
    for root, _dirs, files in os.walk(jobs_dir):
        if "result.json" not in files:
            continue
        path = os.path.join(root, "result.json")
        data = _load_json(path)
        if not data:
            continue
        err = _extract_adapter_error(data)
        if err:
            hits.append({"path": path, "error": err})
    return hits


def scan_trial_log(log_path: str) -> list[str]:
    """Fallback: line-anchored adapter markers in trial_output.log."""
    if not log_path or not os.path.isfile(log_path):
        return []
    lines: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if any(m in line for m in ADAPTER_MARKERS):
                    lines.append(line.rstrip())
    except OSError:
        return []
    return lines


def main(argv: list[str] | None = None) -> int:
    args = (argv or sys.argv)[1:]
    jobs_dir = args[0] if args else "jobs"
    log_path = args[1] if len(args) > 1 else "trial_output.log"
    hits = scan_jobs_dir(jobs_dir)
    log_lines = scan_trial_log(log_path)
    if not hits and not log_lines:
        return 0
    print(
        "DEEPSWE_ADAPTER_FAIL: adapter error detected via structured scan "
        "(result.json / trial log)"
    )
    for h in hits[:10]:
        print(f"  {h['path']}: {h['error'][:300]}")
    for ln in log_lines[:5]:
        print(f"  log: {ln[:300]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
