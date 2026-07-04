"""Structured logger wrapper for the example app."""
from __future__ import annotations

import json
import sys
from typing import Any


def log_event(level: str, msg: str, **fields: Any) -> None:
    """Emit a single JSON log line."""
    record = {"level": level, "msg": msg, **fields}
    sys.stdout.write(json.dumps(record) + "\n")


def info(msg: str, **fields: Any) -> None:
    log_event("info", msg, **fields)


def error(msg: str, **fields: Any) -> None:
    log_event("error", msg, **fields)
