#!/usr/bin/env python3
import json, glob, os

base = "/tmp/qwen_fc_ablation/v2_parallel_1777333524"
os.chdir(base)

# Find eval reports anywhere
for arm in ["A", "B", "C", "D", "E"]:
    found = False
    for pattern in [f"{arm}/*.v2_{arm}.json", f"*.v2_{arm}.json", f"{arm}.v2_{arm}.json",
                    f"/home/Lenovo/{arm}.v2_{arm}.json", f"/tmp/SWE-agent/{arm}.v2_{arm}.json"]:
        for f in glob.glob(pattern):
            d = json.load(open(f))
            ids = sorted([i.split("-")[-1] for i in d.get("resolved_ids", [])])
            completed = d.get("completed_instances", "?")
            print(f"{arm}: {len(ids)}/{completed} resolved = {ids}")
            found = True
            break
        if found:
            break
    if not found:
        # Search everywhere
        for f in glob.glob(f"/home/Lenovo/*v2_{arm}*json") + glob.glob(f"/tmp/SWE-agent/*v2_{arm}*json"):
            d = json.load(open(f))
            ids = sorted([i.split("-")[-1] for i in d.get("resolved_ids", [])])
            print(f"{arm}: {len(ids)}/{d.get('completed_instances','?')} resolved = {ids} (from {f})")
            found = True
            break
    if not found:
        print(f"{arm}: eval report not found")
