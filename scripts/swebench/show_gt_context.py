#!/usr/bin/env python3
"""Show the actual GT context that was injected into each task's prompt."""
import json
import os
import glob
import sys

gt_dir = sys.argv[1] if len(sys.argv) > 1 else "/home/Lenovo/results/v8_debug_gt_10t"

for iid_dir in sorted(glob.glob(os.path.join(gt_dir, "*__*"))):
    iid = os.path.basename(iid_dir)
    traj_path = os.path.join(gt_dir, iid, f"{iid}.traj.json")
    if not os.path.exists(traj_path):
        continue

    t = json.load(open(traj_path))
    chars = t.get("info", {}).get("gt_context_chars", 0)

    history = t.get("history", t.get("messages", []))
    if not history:
        continue

    # GT context is in the user message (history[1]), not system (history[0])
    first = ""
    for msg in history[:3]:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if "<gt_codebase_context>" in content:
            first = content
            break

    if "<gt_codebase_context>" in first:
        start = first.index("<gt_codebase_context>")
        end_marker = "</gt_codebase_context>"
        end = first.index(end_marker) + len(end_marker) if end_marker in first else start + 2000
        ctx = first[start:end]
        print(f"=== {iid} ({chars} chars) ===")
        print(ctx)
        print()
    else:
        print(f"=== {iid} (NO GT CONTEXT) ===")
        print()
