#!/usr/bin/env python3
"""Show the v10 ego-graph output from task trajectories."""
import json
import os
import glob
import sys

traj_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/v10_test1"

for iid_dir in sorted(glob.glob(os.path.join(traj_dir, "*__*"))):
    iid = os.path.basename(iid_dir)
    traj_path = os.path.join(traj_dir, iid, f"{iid}.traj.json")
    if not os.path.exists(traj_path):
        continue

    t = json.load(open(traj_path))
    history = t.get("history", t.get("messages", []))

    found = False
    for msg in history[:5]:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if "CONNECTED CODE" in content or "CODEBASE INTELLIGENCE" in content:
            # Extract the GT block
            start = content.find("=== CODEBASE INTELLIGENCE")
            if start < 0:
                start = content.find("--- CONNECTED CODE ---")
            end = content.find("=== END CODEBASE INTELLIGENCE ===")
            if start >= 0:
                block = content[start:end + 35] if end >= 0 else content[start:start + 2000]
                print(f"=== {iid} ({len(block)} chars) ===")
                print(block)
                print()
                found = True
                break

    if not found:
        info = t.get("info", {})
        gt_chars = info.get("gt_context_chars", 0)
        print(f"=== {iid} (NO EGO-GRAPH, gt_chars={gt_chars}) ===")
        print()
