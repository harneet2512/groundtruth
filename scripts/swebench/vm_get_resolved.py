#!/usr/bin/env python3
import json, glob, os
os.chdir("/tmp")
for arm in ["A", "B", "C", "D", "E"]:
    for f in glob.glob(f"*ablation_{arm}*.json"):
        d = json.load(open(f))
        ids = sorted(d.get("resolved_ids", []))
        ids_short = [i.split("-")[-1] for i in ids]
        print(f"{arm}: {len(ids)}/10 resolved — {ids_short}")
        break
