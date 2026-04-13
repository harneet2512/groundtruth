#!/usr/bin/env python3
"""Full regression matrix across ALL session runs."""
import json
import glob

H = {
    "BL":       {12907:1,13033:0,13236:0,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
    "v2(Qwen)": {12907:1,13033:0,13236:1,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
    "v3(Qwen)": {12907:1,13033:0,13236:0,13398:0,13453:1,13579:0,13977:0,14096:0,14182:0,14309:1},
    "v6(Qwen)": {12907:1,13033:0,13236:0,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
}

base = "/home/Lenovo/logs/run_evaluation"
run_map = {
    "DS-tools": f"{base}/tools_only/openai__deepseek-v3",
    "DS-v1":    f"{base}/hybrid_v1/openai__deepseek-v3",
    "DS-v2":    f"{base}/hybrid_v2/openai__deepseek-v3",
    "DS-v2+q":  f"{base}/qual_v2_full_q1/openai__deepseek-v3",
    "Gate-r1":  f"{base}/gate_r1_full/openai__deepseek-v3",
    "Gate-r2":  f"{base}/gate_r2_full/openai__deepseek-v3",
    "DS-v3":    f"{base}/dsv3_r1/openai__deepseek-v3",
}

for label, path in run_map.items():
    results = {}
    for f in glob.glob(f"{path}/astropy__astropy-*/report.json"):
        d = json.load(open(f))
        for k, v in d.items():
            t = int(k.split("-")[-1])
            results[t] = 1 if v.get("resolved", False) else 0
    if results:
        H[label] = results

tasks = [12907, 13033, 13236, 13398, 13453, 13579, 13977, 14096, 14182, 14309]
versions = list(H.keys())

print(f"{'Task':>7}", end="")
for v in versions:
    print(f" | {v:>8}", end="")
print(" | Pattern")
print("-" * (10 + 11 * len(versions) + 12))

for t in tasks:
    bl = H["BL"].get(t, 0)
    print(f"{t:>7}", end="")
    for v in versions:
        val = H[v].get(t, -1)
        if val == -1:
            m = "  ?  "
        elif val and not bl:
            m = " +Y+ "
        elif not val and bl:
            m = " -N- "
        elif val:
            m = "  Y  "
        else:
            m = "  N  "
        print(f" | {m:>8}", end="")
    pat = {12907: "always", 13033: "never", 13236: "flip", 13398: "never",
           13453: "always", 13579: "v3reg", 13977: "never", 14096: "infra",
           14182: "never", 14309: "always"}.get(t, "")
    print(f" | {pat}")

print("-" * (10 + 11 * len(versions) + 12))
print(f"{'TOTAL':>7}", end="")
for v in versions:
    total = sum(H[v].get(t, 0) for t in tasks if H[v].get(t, -1) != -1)
    count = sum(1 for t in tasks if H[v].get(t, -1) != -1)
    print(f" | {total:>3}/{count:<3} ", end="")
print()

print()
print("=== CHANGES FROM BASELINE ===")
for v in versions:
    if v == "BL":
        continue
    gained = [t for t in tasks if H[v].get(t, 0) and not H["BL"].get(t, 0)]
    lost = [t for t in tasks if not H[v].get(t, 0) and H["BL"].get(t, 0) and H[v].get(t, -1) != -1]
    total = sum(H[v].get(t, 0) for t in tasks if H[v].get(t, -1) != -1)
    count = sum(1 for t in tasks if H[v].get(t, -1) != -1)
    print(f"  {v:>10}: {total}/{count}", end="")
    if gained:
        print(f"  GAINED {gained}", end="")
    if lost:
        print(f"  LOST {lost}", end="")
    if not gained and not lost:
        print("  identical to BL", end="")
    print()
