"""Non-invasive timing wrapper around gt_hook.py.

Records per-fire wall-clock buckets and emits a single JSON line to
``$GT_TIMING_OUT`` (default ``/tmp/gt_hook_timing.jsonl``). gt_hook.py is
imported lazily so its heavy import cost lands inside the ``wall_import_ms``
bucket, not the interpreter-startup bucket. stdout from gt_hook flows
through untouched; the wrapper exits with gt_hook's exit code.

Usage:
    GT_TIMING_OUT=/tmp/gt_hook_timing.jsonl GT_TIMING_EDIT_IDX=3 \
        python gt_hook_timed.py --db=/tmp/graph.db --file=foo.py \
            --function=bar --reminder
"""

import time
t0 = time.perf_counter()

import json
import os
import sys
from datetime import datetime, timezone

t1 = time.perf_counter()

# Ensure gt_hook.py (sibling file) is importable when invoked as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import importlib
gt_hook_mod = importlib.import_module("gt_hook")

t2 = time.perf_counter()


def _emit_timing(t0_, t1_, t2_, t3_, t4_):
    """Write a single JSON line with timing buckets. Never raises."""
    try:
        out_path = os.environ.get("GT_TIMING_OUT", "/tmp/gt_hook_timing.jsonl")
        try:
            edit_idx = int(os.environ.get("GT_TIMING_EDIT_IDX", "0"))
        except ValueError:
            edit_idx = 0
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "edit_idx": edit_idx,
            "wall_total_ms": int(round((t4_ - t0_) * 1000)),
            "wall_python_startup_ms": int(round((t1_ - t0_) * 1000)),
            "wall_import_ms": int(round((t2_ - t1_) * 1000)),
            "wall_pre_main_ms": int(round((t3_ - t2_) * 1000)),
            "wall_main_ms": int(round((t4_ - t3_) * 1000)),
        }
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 — never crash the hook on telemetry
        try:
            sys.stderr.write(f"[gt_hook_timed] timing write failed: {exc}\n")
        except Exception:
            pass


def _run():
    rc = 0
    t3 = time.perf_counter()
    try:
        gt_hook_mod.main()
    except SystemExit as se:
        rc = int(se.code) if isinstance(se.code, int) else (0 if se.code is None else 1)
    except Exception:
        # gt_hook's __main__ swallows exceptions; preserve that contract.
        rc = 0
    t4 = time.perf_counter()
    _emit_timing(t0, t1, t2, t3, t4)
    sys.exit(rc)


if __name__ == "__main__":
    _run()
