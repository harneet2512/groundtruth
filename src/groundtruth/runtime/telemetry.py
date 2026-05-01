"""Unified JSONL telemetry writer for full-form GT runtime layers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

TELEMETRY_FILE = "gt_runtime_telemetry.jsonl"


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_log_dir() -> Path:
    return Path(os.environ.get("GT_LOG_DIR", "/tmp/gt_logs"))


def append_block(
    block: str,
    data: dict[str, Any],
    *,
    log_dir: str | None = None,
    task_id: str = "unknown",
) -> str | None:
    """Append one unified telemetry block.

    The record has a stable outer envelope and a single ``gt_*`` payload key so
    benchmark scrapers can count participation without understanding every
    payload schema.
    """
    target_dir = Path(log_dir) if log_dir else default_log_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    payload = {
        "timestamp": utc_timestamp(),
        "task_id": task_id,
        "block": block,
        block: data,
    }
    target = target_dir / TELEMETRY_FILE
    try:
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        return str(target)
    except (OSError, TypeError, ValueError):
        return None


def read_blocks(path: str | Path) -> list[dict[str, Any]]:
    """Read telemetry JSONL records, skipping malformed lines."""
    records: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records
