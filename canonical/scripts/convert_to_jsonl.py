#!/usr/bin/env python3
"""Convert SWE-agent trajectory output to SWE-bench submission JSONL.

Reads trajectory files from a run directory and produces a clean
all_preds.jsonl suitable for SWE-bench evaluation.

Usage:
    python canonical/scripts/convert_to_jsonl.py \\
        --input-dir results/canonical_gt_v1 \\
        --output results/canonical_gt_v1/submission/all_preds.jsonl \\
        --model-name "sweagent+deepseek-v3.2+groundtruth"
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("convert")


def extract_patch_from_traj(traj_path: Path) -> str:
    """Extract patch from a SWE-agent trajectory file."""
    try:
        data = json.loads(traj_path.read_text())
    except Exception:
        return ""

    # Check info.submission first (standard SWE-agent output)
    info = data.get("info", {})
    submission = info.get("submission", "")
    if submission and submission.startswith("diff"):
        return submission

    # Fallback: search messages for COMPLETE_TASK_AND_SUBMIT pattern
    messages = data.get("messages", [])
    for msg in reversed(messages):
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in content:
            # Extract everything after the marker
            idx = content.index("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
            after = content[idx + len("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"):].strip()
            if after.startswith("diff"):
                return after

    # Fallback: look for diff blocks in output
    for msg in reversed(messages):
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        m = re.search(r'(diff --git .*?)(?:\n(?:```|\Z))', content, re.DOTALL)
        if m:
            return m.group(1)

    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert trajectories to submission JSONL")
    parser.add_argument("--input-dir", required=True, help="Run output directory")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--model-name", default="sweagent+deepseek-v3.2+groundtruth")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Find trajectory files
    traj_files = sorted(input_dir.rglob("*.traj.json"))
    if not traj_files:
        traj_files = sorted(input_dir.rglob("*.traj"))

    logger.info("Found %d trajectory files in %s", len(traj_files), input_dir)

    entries = []
    empty = 0
    for traj_path in traj_files:
        instance_id = traj_path.stem.replace(".traj", "")
        patch = extract_patch_from_traj(traj_path)

        if not patch:
            empty += 1
            logger.warning("Empty patch: %s", instance_id)

        entries.append({
            "instance_id": instance_id,
            "model_patch": patch,
            "model_name_or_path": args.model_name,
        })

    # Write JSONL
    with open(output_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    logger.info("Wrote %d predictions to %s (%d empty patches)",
                len(entries), output_path, empty)


if __name__ == "__main__":
    main()
