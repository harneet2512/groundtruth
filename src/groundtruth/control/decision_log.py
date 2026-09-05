"""KernelEvent -> unified telemetry block writer.

This module routes ``KernelEvent`` records through
``runtime.telemetry.append_block`` under the block name
``gt_kernel_decision``. Schema specified in ``docs/kernel/telemetry.md``.

Plumbing safety (B3 from the Phase 1 plan):
``append_block`` is atomic per JSONL line but NOT atomic across concurrent
calls. We wrap each write in a ``filelock.FileLock`` keyed on the log_dir
so multi-threaded kernel use cannot interleave records mid-line.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from filelock import FileLock

from groundtruth.control.types import ErrorClass, KernelEvent
from groundtruth.runtime.telemetry import append_block

KERNEL_DECISION_BLOCK = "gt_kernel_decision"
KERNEL_PULL_BLOCK = "gt_pull"

__all__ = ["KERNEL_DECISION_BLOCK", "KERNEL_PULL_BLOCK", "ErrorClass", "append_decision"]


def _lock_path(log_dir: str | None) -> str | None:
    base = log_dir or os.environ.get("GT_LOG_DIR") or tempfile.gettempdir()
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
    except (OSError, FileExistsError):
        return None
    return str(Path(base) / ".gt_kernel_decision.lock")


def append_decision(event: KernelEvent, *, log_dir: str | None = None) -> str | None:
    """Append a Decision Trace 7-element record under a per-log-dir FileLock.

    Returns the path written to (per ``append_block`` contract) or ``None``
    on failure. Failures are not raised because telemetry must never crash
    the agent loop.
    """
    payload = event.model_dump(mode="json")
    inner = {
        "triggering_state": payload["triggering_state"],
        "context_evaluated": payload["context_evaluated"],
        "policy_applied": payload["policy_applied"],
        "alternatives_considered": payload["alternatives_considered"],
        "confidence": payload["confidence"],
        "action_selected": payload["action_selected"],
        "authority_exercised": payload["authority_exercised"],
    }
    def _do_append() -> str | None:
        try:
            return append_block(
                KERNEL_DECISION_BLOCK,
                inner,
                log_dir=log_dir,
                task_id=event.task_id,
            )
        except Exception:
            return None

    lock_path = _lock_path(log_dir)
    if lock_path is None:
        # log_dir is unwritable -- skip the lock; append_block will return
        # None per its OSError contract.
        return _do_append()
    try:
        with FileLock(lock_path, timeout=10):
            return _do_append()
    except Exception:
        return None
