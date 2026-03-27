#!/usr/bin/env python3
"""Show eval results for baseline and GT v8."""
import json
import glob
import os

RESULTS = "/root/results/v7_smoke_20260327_181052"

for label, subdir, note in [
    ("BASELINE", "baseline", "v7_baseline"),
    ("GT V8", "gt_v7", "v8_gt"),
]:
    pattern = os.path.join(RESULTS, subdir, "**", "output.report.json")
    files = glob.glob(pattern, recursive=True)
    if not files:
        print(f"=== {label} === NO REPORT FOUND")
        continue

    d = json.load(open(files[0]))
    resolved = d.get("resolved_ids", [])
    unresolved = d.get("unresolved_ids", [])
    error = d.get("error_ids", [])
    total = len(resolved) + len(unresolved) + len(error)

    print(f"=== {label} ===")
    print(f"  Resolved: {len(resolved)}/{total} ({100*len(resolved)/total:.1f}%)")
    print(f"  Resolved: {sorted(resolved)}")
    print(f"  Unresolved: {sorted(unresolved)}")
    print(f"  Errors: {sorted(error)}")
    print()

# Understand call analysis
print("=== UNDERSTAND CALL ANALYSIS ===")
gt_logs = os.path.join(RESULTS, "gt_v7", "**", "logs", "instance_*.output.log")
for f in sorted(glob.glob(gt_logs, recursive=True)):
    inst = os.path.basename(f).replace("instance_", "").replace(".output.log", "")
    with open(f) as fh:
        content = fh.read()
    u = content.count("gt_hook.py understand")
    v = content.count("gt_hook.py verify")
    if u > 0 or v > 0:
        print(f"  {inst}: {u} understand, {v} verify")
