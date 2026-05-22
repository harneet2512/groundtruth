#!/usr/bin/env python3
"""task_result_summarizer.py -- Summarize eval results per task.

Reads eval_result.json files from run artifacts and produces a per-task summary
with resolved/not-resolved status, patch info, and action counts.

Usage:
    python scripts/verify/task_result_summarizer.py --output-dir /path/to/artifacts

Output: JSON to stdout with per-task and aggregate summary.
Exit 0 on success, 1 on script error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def find_files_by_name(output_dir: str, name: str) -> list[Path]:
    """Find all files with a given name in the output directory tree."""
    results = []
    for root, _dirs, files in os.walk(output_dir):
        for f in files:
            if f == name:
                results.append(Path(root) / f)
    return sorted(results)


def infer_task_id_from_path(path: Path) -> str:
    """Infer task ID from artifact path."""
    for part in path.parts:
        if part.startswith("task-"):
            return part.replace("task-", "", 1)
        if "__" in part and any(c.isdigit() for c in part):
            return part
    return path.parent.name


def parse_eval_result(eval_path: Path) -> dict:
    """Parse an eval_result.json file and extract resolution status."""
    try:
        data = json.loads(eval_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "resolved": None,
            "status": "error",
            "error": str(exc),
        }

    # Multiple possible formats from the eval harness
    resolved = False
    status = "unknown"

    if isinstance(data.get("resolved_instances"), int):
        resolved = data["resolved_instances"] > 0
        status = "resolved" if resolved else "not_resolved"
    elif "resolved_ids" in data:
        resolved = bool(data["resolved_ids"])
        status = "resolved" if resolved else "not_resolved"
    elif isinstance(data.get("resolved"), list):
        resolved = bool(data["resolved"])
        status = "resolved" if resolved else "not_resolved"
    elif isinstance(data, dict):
        # Per-instance format: {"instance_id": {"resolved": true/false, ...}}
        for k, v in data.items():
            if isinstance(v, dict) and "resolved" in v:
                resolved = bool(v["resolved"])
                status = "resolved" if resolved else "not_resolved"
                break
    else:
        status = data.get("status", "unknown")

    return {
        "resolved": resolved,
        "status": status,
        "raw_keys": list(data.keys()) if isinstance(data, dict) else [],
    }


def extract_action_count(output_jsonl: Path | None) -> dict:
    """Extract action count and edit count from output.jsonl."""
    if output_jsonl is None or not output_jsonl.exists():
        return {"actions": None, "edits": None, "gt_visible": None}

    try:
        with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline().strip()
            if not first_line:
                return {"actions": 0, "edits": 0, "gt_visible": 0}
            record = json.loads(first_line)
    except (json.JSONDecodeError, OSError):
        return {"actions": None, "edits": None, "gt_visible": None}

    history = record.get("history", [])
    if not isinstance(history, list):
        return {"actions": None, "edits": None, "gt_visible": None}

    actions = [
        e for e in history
        if e.get("action") and e.get("action") not in ("think", "recall", "message")
    ]
    edits = [
        e for e in history
        if e.get("action") in ("edit", "write") or "str_replace" in str(e.get("args", {}))
    ]
    gt_visible = sum(
        1 for e in history
        if "[GT]" in str(e.get("content", "") or e.get("message", ""))
    )

    return {
        "actions": len(actions),
        "edits": len(edits),
        "gt_visible": gt_visible,
    }


def extract_patch_info(output_jsonl: Path | None) -> dict:
    """Extract patch presence and size from output.jsonl."""
    if output_jsonl is None or not output_jsonl.exists():
        return {"patch_produced": False, "patch_chars": 0}

    try:
        with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                patch = (
                    record.get("git_patch", "")
                    or record.get("test_result", {}).get("git_patch", "")
                    or ""
                )
                if patch and "diff" in patch:
                    return {"patch_produced": True, "patch_chars": len(patch)}
    except OSError:
        pass
    return {"patch_produced": False, "patch_chars": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize eval results per task")
    parser.add_argument("--output-dir", required=True, help="Root directory with run artifacts")
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        print(json.dumps({"error": f"Not a directory: {output_dir}"}))
        return 1

    # Find all eval_result.json and output.jsonl files
    eval_files = find_files_by_name(output_dir, "eval_result.json")
    output_files = find_files_by_name(output_dir, "output.jsonl")

    # Build task_id -> files mapping
    task_eval: dict[str, Path] = {}
    for ef in eval_files:
        tid = infer_task_id_from_path(ef)
        task_eval[tid] = ef

    task_output: dict[str, Path] = {}
    for of in output_files:
        tid = infer_task_id_from_path(of)
        task_output[tid] = of

    all_task_ids = sorted(set(list(task_eval.keys()) + list(task_output.keys())))

    if not all_task_ids:
        print(json.dumps({"error": "No eval_result.json or output.jsonl found", "dir": output_dir}))
        return 1

    results = []
    for tid in all_task_ids:
        eval_path = task_eval.get(tid)
        output_path = task_output.get(tid)

        eval_info = parse_eval_result(eval_path) if eval_path else {
            "resolved": None, "status": "no_eval_file"
        }
        action_info = extract_action_count(output_path)
        patch_info = extract_patch_info(output_path)

        results.append({
            "task_id": tid,
            "resolved": eval_info["resolved"],
            "status": eval_info["status"],
            "patch_produced": patch_info["patch_produced"],
            "patch_chars": patch_info["patch_chars"],
            "actions": action_info["actions"],
            "edits": action_info["edits"],
            "gt_visible": action_info["gt_visible"],
            "eval_file": str(eval_path) if eval_path else None,
            "output_file": str(output_path) if output_path else None,
        })

    total = len(results)
    resolved = sum(1 for r in results if r["resolved"] is True)
    patched = sum(1 for r in results if r["patch_produced"])
    errored = sum(1 for r in results if r["status"] == "error")

    output = {
        "check": "task_result_summarizer",
        "total_tasks": total,
        "resolved": resolved,
        "patched": patched,
        "not_resolved": total - resolved - errored,
        "errors": errored,
        "resolve_rate": f"{resolved}/{total}" if total > 0 else "0/0",
        "patch_rate": f"{patched}/{total}" if total > 0 else "0/0",
        "results": results,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
