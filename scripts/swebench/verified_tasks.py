#!/usr/bin/env python3
"""Generate SWE-bench Verified task lists and GHA matrix JSON.

Reads the canonical task list from benchmarks/swebench_verified_500_ids.json
(version-controlled, deterministic sort). No HuggingFace dependency at runtime.

Usage:
    # GHA matrix (default)
    python scripts/swebench/verified_tasks.py --mode smoke --tasks-per-job 3

    # Plain list
    python scripts/swebench/verified_tasks.py --mode full500 --output list

    # Just the count
    python scripts/swebench/verified_tasks.py --mode pilot100 --output count
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_MODE_COUNTS = {
    "smoke": 5,
    "pilot20": 20,
    "pilot50": 50,
    "pilot100": 100,
    "full500": None,  # all
}

_IDS_FILE = Path(__file__).resolve().parent.parent.parent / "benchmarks" / "swebench_verified_500_ids.json"


def load_task_ids(count: int | None = None) -> list[str]:
    if not _IDS_FILE.exists():
        print(f"ERROR: {_IDS_FILE} not found. Run fetch_verified_ids.py first.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(_IDS_FILE.read_text(encoding="utf-8"))
    ids: list[str] = data["instance_ids"]
    if count is not None:
        ids = ids[:count]
    return ids


def build_matrix(task_ids: list[str], tasks_per_job: int) -> dict:
    batches = []
    for i in range(0, len(task_ids), tasks_per_job):
        batch = task_ids[i : i + tasks_per_job]
        batches.append({"batch_id": i // tasks_per_job, "tasks": ",".join(batch)})
    return {"include": batches}


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench Verified task list generator")
    parser.add_argument(
        "--mode",
        choices=list(_MODE_COUNTS.keys()),
        default="smoke",
        help="Run mode (smoke=5, pilot20=20, pilot50=50, pilot100=100, full500=all)",
    )
    parser.add_argument(
        "--tasks-per-job",
        type=int,
        default=3,
        help="Tasks per GHA matrix job (default: 3)",
    )
    parser.add_argument(
        "--output",
        choices=["matrix", "list", "count"],
        default="matrix",
        help="Output format (default: matrix)",
    )
    parser.add_argument(
        "--task-ids",
        default="",
        help="Comma-separated explicit instance IDs. Overrides --mode when set "
        "(for targeted re-runs / eval validation).",
    )
    args = parser.parse_args()

    if args.task_ids.strip():
        task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]
    else:
        count = _MODE_COUNTS[args.mode]
        task_ids = load_task_ids(count)

    if args.output == "count":
        print(len(task_ids))
    elif args.output == "list":
        for tid in task_ids:
            print(tid)
    elif args.output == "matrix":
        matrix = build_matrix(task_ids, args.tasks_per_job)
        print(json.dumps(matrix, separators=(",", ":")))


if __name__ == "__main__":
    main()
