"""aggregate.py — classify a directory of per-task contract dirs into the report.

Reused by the 10-task probe and the 300-task dry pipeline: reads each task's
downloaded contract dir (gt-contracts-<task>/gt_contracts/*.json), classifies with
classify.py (top_level_class), and writes the report + summary csv. Read-only.

Usage:
  python -m reliability.aggregate <probe_dir> <gate_map_file> <out_dir> [title]
where gate_map_file has one `task_id=success|failure|pending` per line (the
in-container gate verdict from the workflow job conclusions).
"""
from __future__ import annotations

import json
import os
import sys

from .classify import classify_task_dir
from .report import write_all


def _contract_dir(probe_dir: str, d: str) -> str:
    inner = os.path.join(probe_dir, d, "gt_contracts")
    return inner if os.path.isdir(inner) else os.path.join(probe_dir, d)


def aggregate(probe_dir: str, gate_map: dict[str, str], out_dir: str,
              title: str = "10-task probe") -> dict:
    tasks: dict[str, dict] = {}
    for d in sorted(os.listdir(probe_dir)):
        if not d.startswith("gt-contracts-"):
            continue
        task = d[len("gt-contracts-"):]
        cdir = _contract_dir(probe_dir, d)
        verdict = gate_map.get(task, "pending")
        gate_passed = verdict == "success"
        res = classify_task_dir(cdir, gate_passed)
        res["hook_status"] = "N/A"
        res["gate_verdict"] = verdict
        tasks[task] = res
    os.makedirs(out_dir, exist_ok=True)
    write_all(tasks, out_dir, title)
    json.dump({t: {"final_class": r["final_class"], "top_level_class": r["top_level_class"],
                   "reason": r["reason"]} for t, r in tasks.items()},
              open(os.path.join(out_dir, "classifications.json"), "w"), indent=2)
    return tasks


def _load_gate_map(path: str) -> dict[str, str]:
    m: dict[str, str] = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                m[k.strip()] = v.strip()
    return m


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: aggregate <probe_dir> <gate_map_file> <out_dir> [title]")
        return 2
    probe_dir, gate_file, out_dir = argv[0], argv[1], argv[2]
    title = argv[3] if len(argv) > 3 else "10-task probe"
    tasks = aggregate(probe_dir, _load_gate_map(gate_file), out_dir, title)
    print(f"{'task':30} {'top_level_class':36} final_class")
    for t, r in sorted(tasks.items()):
        print(f"  {t:28} {r['top_level_class']:36} {r['final_class']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
