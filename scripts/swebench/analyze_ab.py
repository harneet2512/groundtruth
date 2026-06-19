#!/usr/bin/env python3
"""Analyze A/B eval results — compare baseline vs GT hooked, check evidence."""
import json
import glob
import os
import sys


def read_preds(path):
    preds = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                p = json.loads(line)
                preds[p["instance_id"]] = p
    return preds


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."

    bl = read_preds(f"{outdir}/baseline/predictions.jsonl")
    gt = read_preds(f"{outdir}/gt_hooked/predictions.jsonl")

    print("=" * 60)
    print("PREDICTIONS SUMMARY")
    print("=" * 60)
    print(f"Baseline predictions: {len(bl)}")
    print(f"GT predictions:       {len(gt)}")

    # Show patch sizes
    for task_id in sorted(set(list(bl.keys()) + list(gt.keys()))):
        bl_patch = bl.get(task_id, {}).get("model_patch", "")
        gt_patch = gt.get(task_id, {}).get("model_patch", "")
        bl_len = len(bl_patch) if bl_patch else 0
        gt_len = len(gt_patch) if gt_patch else 0
        print(f"  {task_id}: baseline={bl_len} chars, gt={gt_len} chars")

    # Analyze GT evidence in trajectories
    print()
    print("=" * 60)
    print("GT EVIDENCE ANALYSIS (per task)")
    print("=" * 60)

    total_evidence = 0
    tasks_with_evidence = 0

    for traj_file in sorted(
        glob.glob(f"{outdir}/gt_hooked/**/*.traj.json", recursive=True)
    ):
        task_id = os.path.basename(traj_file).replace(".traj.json", "")
        with open(traj_file) as f:
            traj = json.load(f)

        messages = traj.get("messages", [])
        info = traj.get("info", {})

        # Extract ALL GT evidence lines
        gt_lines = []
        gt_blocks = 0
        for m in messages:
            c = m.get("content", "")
            if not isinstance(c, str):
                continue
            if "<gt-evidence>" in c:
                gt_blocks += 1
            for line in c.split("\n"):
                line = line.strip()
                if any(
                    tag in line
                    for tag in [
                        "[VERIFIED]",
                        "[WARNING]",
                        "[INFO]",
                        "[OK]",
                        "[STALE]",
                        "[SKIP]",
                        "GT:",
                        "<gt-evidence",
                        "</gt-evidence>",
                    ]
                ):
                    gt_lines.append(line[:150])

        if gt_lines:
            tasks_with_evidence += 1
            total_evidence += len(gt_lines)

        print(f"\n--- {task_id} ---")
        print(f"  Messages: {len(messages)}")
        print(f"  Exit: {info.get('exit_status', '?')}")
        print(f"  GT blocks: {gt_blocks}")
        print(f"  GT evidence lines: {len(gt_lines)}")

        # Show ALL GT evidence
        for line in gt_lines:
            print(f"    | {line}")

        if not gt_lines:
            print("    (no GT evidence in trajectory)")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Tasks with GT evidence: {tasks_with_evidence}/{len(gt)}")
    print(f"Total GT evidence lines: {total_evidence}")
    print(
        f"Avg evidence per task: {total_evidence / max(1, len(gt)):.1f}"
    )


if __name__ == "__main__":
    main()
