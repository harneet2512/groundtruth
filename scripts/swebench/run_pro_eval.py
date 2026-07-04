#!/usr/bin/env python3
"""Convert mini-swe-agent preds.json to SWE-bench Pro eval format and run eval."""
import json
import os
import sys
import csv
import subprocess

preds_path = sys.argv[1]  # path to preds.json
run_id = sys.argv[2]      # e.g. "v11_baseline" or "v11_gt"
output_dir = sys.argv[3] if len(sys.argv) > 3 else f"/home/Lenovo/results/eval_{run_id}"

# Load predictions
preds = json.load(open(preds_path))
print(f"Loaded {len(preds)} predictions from {preds_path}")

# Convert to Pro eval format: list of {instance_id, patch, prefix}
patches = []
for iid, pred in preds.items():
    patch = pred.get("model_patch", "") if isinstance(pred, dict) else ""
    if patch:
        patches.append({
            "instance_id": iid,
            "patch": patch,
            "prefix": "sample1",
        })

patch_path = os.path.join(output_dir, f"{run_id}_patches.json")
os.makedirs(output_dir, exist_ok=True)
with open(patch_path, "w") as f:
    json.dump(patches, f, indent=2)
print(f"Wrote {len(patches)} patches to {patch_path}")

# Create CSV from dataset
from datasets import load_dataset
ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")

# Filter to only instances we have patches for
patch_ids = {p["instance_id"] for p in patches}
csv_path = os.path.join(output_dir, "tasks.csv")
with open(csv_path, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=ds.column_names)
    writer.writeheader()
    for row in ds:
        if row["instance_id"] in patch_ids:
            writer.writerow(row)
print(f"Wrote {len(patch_ids)} tasks to {csv_path}")

# Run eval
eval_script = "/home/Lenovo/SWE-bench_Pro-os/swe_bench_pro_eval.py"
scripts_dir = "/home/Lenovo/SWE-bench_Pro-os/run_scripts"

cmd = [
    sys.executable, eval_script,
    "--raw_sample_path", csv_path,
    "--patch_path", patch_path,
    "--output_dir", output_dir,
    "--scripts_dir", scripts_dir,
    "--dockerhub_username", "jefzda",
    "--use_local_docker",
    "--num_workers", "8",
]
print(f"Running eval: {' '.join(cmd)}")
subprocess.run(cmd)
