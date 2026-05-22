#!/usr/bin/env python3
"""gt_pollution_check.py -- Detect GT debug/meta pollution in agent-visible observations.

Reads output.jsonl from a run and checks every agent-visible observation for
GT_STATUS, GT_META, or other debug markers that should only appear in stderr,
not in content the agent reads.

Usage:
    python scripts/verify/gt_pollution_check.py --output-dir /path/to/task/artifacts

Output: JSON to stdout with pass/fail per task.
Exit 0 on success (even if pollution found), exit 1 on script error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

# Markers that should NEVER appear in agent-visible observation content.
# These are stderr diagnostics; if they reach the agent, it is pollution.
POLLUTION_MARKERS = [
    "[GT_META]",
    "[GT_STATUS]",
    "GT_COST",
    "[GT_DEBUG]",
    "__GT_STRUCTURED__",
    "gt_layer_events",
    "gt_interactions",
    "_emit_structured_event",
    "GTLayerEvent(",
]

# Markers that are acceptable in agent content (GT evidence headers)
ACCEPTABLE_MARKERS = [
    "[GT]",
    "<gt-evidence",
    "</gt-evidence>",
    "[GroundTruth]",
]


def find_output_jsonls(output_dir: str) -> list[Path]:
    """Find all output.jsonl files in the artifact directory tree."""
    results = []
    for root, _dirs, files in os.walk(output_dir):
        for f in files:
            if f == "output.jsonl":
                results.append(Path(root) / f)
    return sorted(results)


def extract_agent_observations(output_jsonl: Path) -> list[dict]:
    """Extract agent-visible observation entries from output.jsonl.

    Returns list of dicts with keys: turn_index, content, source.
    """
    observations = []
    try:
        with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
            for line_num, raw_line in enumerate(fh, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                # OpenHands output.jsonl: top-level has 'history' list
                history = record.get("history", [])
                if isinstance(history, list):
                    for idx, entry in enumerate(history):
                        # Observations are entries with 'observation' or 'message' key
                        content = entry.get("content", "") or entry.get("message", "") or ""
                        obs_type = entry.get("observation", entry.get("action", ""))
                        if content and isinstance(content, str):
                            observations.append({
                                "turn_index": idx,
                                "content": content,
                                "source": f"line={line_num},idx={idx},type={obs_type}",
                            })
                # Single-entry format (one record per line)
                else:
                    content = record.get("content", "") or record.get("message", "") or ""
                    obs_type = record.get("observation", record.get("action", ""))
                    if content and isinstance(content, str):
                        observations.append({
                            "turn_index": line_num,
                            "content": content,
                            "source": f"line={line_num},type={obs_type}",
                        })
    except OSError as exc:
        print(f"ERROR: cannot read {output_jsonl}: {exc}", file=sys.stderr)
    return observations


def check_pollution(observations: list[dict]) -> list[dict]:
    """Check observations for pollution markers.

    Returns list of violations: {marker, source, excerpt}.
    """
    violations = []
    for obs in observations:
        content = obs["content"]
        for marker in POLLUTION_MARKERS:
            if marker in content:
                # Find the line containing the marker for context
                for line in content.splitlines():
                    if marker in line:
                        violations.append({
                            "marker": marker,
                            "source": obs["source"],
                            "excerpt": line[:200].strip(),
                        })
                        break
    return violations


def infer_task_id(output_jsonl: Path) -> str:
    """Infer task_id from directory name or output.jsonl content."""
    # Try parent dir name pattern: task-amoffat__sh-744/...
    for part in output_jsonl.parts:
        if part.startswith("task-"):
            return part.replace("task-", "", 1)
        # GHA artifact pattern: the dir often contains the task ID
        if "__" in part and any(c.isdigit() for c in part):
            return part
    # Fallback: try reading the first line of output.jsonl
    try:
        with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
            if first:
                d = json.loads(first)
                return d.get("instance_id", d.get("task_id", str(output_jsonl.parent.name)))
    except Exception:
        pass
    return str(output_jsonl.parent.name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check GT pollution in agent observations")
    parser.add_argument("--output-dir", required=True, help="Root directory with run artifacts")
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        print(json.dumps({"error": f"Not a directory: {output_dir}"}))
        return 1

    output_files = find_output_jsonls(output_dir)
    if not output_files:
        print(json.dumps({"error": "No output.jsonl files found", "dir": output_dir}))
        return 1

    results = []
    overall_pass = True

    for ojf in output_files:
        task_id = infer_task_id(ojf)
        observations = extract_agent_observations(ojf)
        violations = check_pollution(observations)

        task_pass = len(violations) == 0
        if not task_pass:
            overall_pass = False

        results.append({
            "task_id": task_id,
            "output_jsonl": str(ojf),
            "observations_checked": len(observations),
            "violations": len(violations),
            "status": "pass" if task_pass else "fail",
            "details": violations[:10],  # cap at 10 per task
        })

    output = {
        "check": "gt_pollution_check",
        "overall_status": "pass" if overall_pass else "fail",
        "tasks_checked": len(results),
        "tasks_with_pollution": sum(1 for r in results if r["status"] == "fail"),
        "results": results,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
