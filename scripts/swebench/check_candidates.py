#!/usr/bin/env python3
"""Check if candidate tasks will produce actionable GT evidence."""
import re
from datasets import load_dataset

ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")

tasks = [
    "reflex-dev__reflex-4711",
    "keras-team__keras-20396",
    "python-babel__babel-1164",
    "pdm-project__pdm-3419",
    "python-telegram-bot__python-telegram-bot-4673",
    "beetbox__beets-5457",
    "pypsa__pypsa-1112",
    "run-llama__llama_deploy-372",
    "stanfordnlp__dspy-1801",
    "mikedh__trimesh-2363",
]

for row in ds:
    if row["instance_id"] not in tasks:
        continue
    iid = row["instance_id"]
    patch = row.get("patch", "")

    funcs = []
    cur_file = None
    for line in patch.split("\n"):
        if line.startswith("--- a/"):
            cur_file = line[6:]
        if line.startswith("@@") and cur_file and "/test" not in cur_file:
            m = re.search(r"def (\w+)", line)
            if m:
                funcs.append(f"{cur_file}:{m.group(1)}")

    dels = sum(1 for l in patch.split("\n") if l.startswith("-") and not l.startswith("---"))
    adds = sum(1 for l in patch.split("\n") if l.startswith("+") and not l.startswith("+++"))

    quality = "GOOD" if funcs else "WEAK"
    print(f"{iid}: {quality}")
    print(f"  Functions: {funcs[:5]}")
    print(f"  -{dels}/+{adds}")
    print()
