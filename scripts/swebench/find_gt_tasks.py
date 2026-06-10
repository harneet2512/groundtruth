#!/usr/bin/env python3
"""Find SWE-bench Live Lite tasks where GT will produce actionable evidence.

Heuristic: tasks where the gold patch MODIFIES existing functions in source files
(not just adds new code) are more likely to touch functions with callers/imports,
which means GT verify will produce [VERIFIED]/[WARNING] instead of [OK].
"""
from datasets import load_dataset

ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")

candidates = []
for row in ds:
    iid = row["instance_id"]
    patch = row.get("patch", "")

    mod_lines = 0
    add_lines = 0
    mod_files = []
    for line in patch.split("\n"):
        if line.startswith("--- a/") and "/test" not in line.lower():
            mod_files.append(line[6:])
        if line.startswith("-") and not line.startswith("---"):
            mod_lines += 1
        if line.startswith("+") and not line.startswith("+++"):
            add_lines += 1

    if mod_lines >= 3 and len(mod_files) >= 1:
        candidates.append({
            "id": iid,
            "repo": iid.split("__")[0],
            "mod_lines": mod_lines,
            "add_lines": add_lines,
            "mod_files": len(mod_files),
            "files": mod_files[:3],
        })

candidates.sort(key=lambda x: -x["mod_lines"])

print(f"Total candidates with >=3 modified lines: {len(candidates)}")
print()
print("Top 20 (most modifications to existing source):")
for c in candidates[:20]:
    print(f"  {c['id']}: -{c['mod_lines']}/+{c['add_lines']} in {c['files']}")
