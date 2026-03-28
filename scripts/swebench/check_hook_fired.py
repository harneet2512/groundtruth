#!/usr/bin/env python3
"""Check if GT hook output appeared in agent trajectories."""
import json
import glob
import os
import sys

traj_dir = sys.argv[1] if len(sys.argv) > 1 else "/home/Lenovo/results/v10_hooked_5t"

for tdir in sorted(glob.glob(os.path.join(traj_dir, "*__*"))):
    iid = os.path.basename(tdir)
    tpath = os.path.join(tdir, iid + ".traj.json")
    if not os.path.exists(tpath):
        print(iid + ": no trajectory")
        continue

    t = json.load(open(tpath))
    history = t.get("messages", t.get("history", []))
    info = t.get("info", {})

    gt_count = 0
    gt_snippets = []
    for msg in history:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if "GT CODEBASE" in content or "CONNECTED CODE" in content:
            gt_count += 1
            # Extract the GT block
            start = content.find("=== GT CODEBASE INTELLIGENCE ===")
            if start < 0:
                start = content.find("--- CONNECTED CODE ---")
            if start >= 0:
                snippet = content[start:start + 200]
                gt_snippets.append(snippet)

    exit_status = info.get("exit_status", "?")
    hook_ok = info.get("hook_injected", "?")
    print(iid)
    print("  turns=" + str(len(history)) + " exit=" + str(exit_status) + " hook=" + str(hook_ok))
    print("  GT fired: " + str(gt_count) + " times")
    if gt_snippets:
        for s in gt_snippets[:2]:
            print("  >> " + s.replace("\n", " | ")[:150])
    print()
