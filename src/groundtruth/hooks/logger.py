"""JSONL hook logger with per-signal detail. Stdlib only — no structlog."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

HOOK_LOG = os.path.join(tempfile.gettempdir(), "gt_hook_log.jsonl")

_log = logging.getLogger("groundtruth.hooks")


def log_hook(entry: dict) -> None:
    """Append one JSON line to the hook log. Never raises."""
    try:
        entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(HOOK_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def get_logger(name: str) -> logging.Logger:
    """Get a stdlib logger (structlog-free for container compatibility)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
    return logger
