"""RC-08 silent-failure counter — make every `except Exception: pass` countable.

A single helper used at every site that previously swallowed an exception with
no signal. Two side effects per call:

  1. ``logger.exception(...)`` — full traceback at WARNING (or DEBUG when the
     caller passes ``debug=True`` for known-low-signal sites).
  2. Append one JSON line to ``$GT_SILENT_FAILURES_FILE`` (if set) describing
     the failure. ``verify_report.py`` reads this file to compute the
     ``silent_failures`` per-task gate.

Both side effects are themselves wrapped in a tiny inner ``try/except`` — if
the counter file itself can't be written we fall back to ``stderr`` so we
never trade one silent failure for another. There is no in-process global
counter: the file is the source of truth across subprocesses (gt-index Go
binary, gt_edit state command bash, hook subprocesses), which is why the
contract is **append-one-JSON-line-per-failure**.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from typing import Any

_ENV = "GT_SILENT_FAILURES_FILE"
_logger = logging.getLogger("groundtruth.silent_failures")


def record(site: str, exc: BaseException | None = None,
           extra: dict[str, Any] | None = None, debug: bool = False) -> None:
    """Count + log a silent failure. ``site`` is a short stable id like
    ``"v22_brief.rank_files"``. Never raises."""
    level = logging.DEBUG if debug else logging.WARNING
    try:
        if exc is not None:
            _logger.log(level, "silent_failure site=%s exc=%s",
                        site, exc, exc_info=exc)
        else:
            _logger.log(level, "silent_failure site=%s", site)
    except Exception:  # pragma: no cover — logger itself broken
        pass

    path = os.environ.get(_ENV)
    if not path:
        return
    rec = {
        "ts": time.time(),
        "site": site,
        "exc_type": type(exc).__name__ if exc is not None else None,
        "exc_msg": str(exc)[:500] if exc is not None else None,
        "tb": traceback.format_exc(limit=3) if exc is not None else None,
        "extra": extra or {},
        "pid": os.getpid(),
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as inner:  # pragma: no cover
        print(f"[silent_failures] could not append to {path}: {inner}",
              file=sys.stderr)


def count_from_file(path: str) -> tuple[int, int]:
    """Read ``path`` and return (records, parse_failures). File missing → (0, 0).
    File present but a line is corrupt → counted in parse_failures, not raise."""
    if not path or not os.path.isfile(path):
        return 0, 0
    records = 0
    parse_failures = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    records += 1
                except Exception:
                    parse_failures += 1
    except OSError:
        return 0, 0
    return records, parse_failures
