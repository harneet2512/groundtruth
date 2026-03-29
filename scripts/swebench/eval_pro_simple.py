#!/usr/bin/env python3
"""Simple SWE-bench Pro evaluator — apply patch, run test, check result.

Bypasses the official eval harness (which needs missing Dockerfiles).
Uses pre-pulled Docker images directly.

Usage:
    python3 eval_pro_simple.py /path/to/preds.json eval_output_dir
"""
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datasets import load_dataset

preds_path = sys.argv[1]
output_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/eval_pro"
max_workers = int(sys.argv[3]) if len(sys.argv) > 3 else 6
scripts_dir = "/home/Lenovo/SWE-bench_Pro-os/run_scripts"

os.makedirs(output_dir, exist_ok=True)

# Load predictions
preds = json.load(open(preds_path))
print(f"Loaded {len(preds)} predictions")

# Load dataset for dockerhub tags
ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
instance_map = {}
for row in ds:
    instance_map[row["instance_id"]] = {
        "dockerhub_tag": row.get("dockerhub_tag", ""),
        "repo": row.get("repo", ""),
    }


def eval_instance(instance_id, patch_text):
    """Apply patch in Docker container and run test script. Returns (instance_id, passed, error)."""
    info = instance_map.get(instance_id)
    if not info or not info["dockerhub_tag"]:
        return instance_id, False, "no dockerhub_tag"

    image = f"jefzda/sweap-images:{info['dockerhub_tag']}"
    script_dir = os.path.join(scripts_dir, instance_id)
    run_script = os.path.join(script_dir, "run_script.sh")

    if not os.path.exists(run_script):
        return instance_id, False, "no run_script.sh"

    if not patch_text or len(patch_text.strip()) < 10:
        return instance_id, False, "empty patch"

    # Write patch to temp file
    patch_file = os.path.join(output_dir, f"{instance_id}.patch")
    with open(patch_file, "w") as f:
        f.write(patch_text)

    try:
        # Run in container: apply patch, run test script, check exit code
        container_name = f"eval-{instance_id[:30]}-{os.getpid()}"
        cmd = [
            "docker", "run", "--rm",
            "--name", container_name,
            "--entrypoint", "",
            "-v", f"{patch_file}:/tmp/patch.diff:ro",
            "-v", f"{run_script}:/tmp/run_test.sh:ro",
            image,
            "bash", "-c",
            "cd /app && "
            "git apply /tmp/patch.diff 2>/dev/null || patch -p1 < /tmp/patch.diff 2>/dev/null; "
            "bash /tmp/run_test.sh 2>&1 | tail -20; "
            "echo EXIT_CODE=$?"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        output = result.stdout + result.stderr

        # Check if tests passed
        if "EXIT_CODE=0" in output:
            return instance_id, True, ""
        else:
            # Extract last few lines for debugging
            last_lines = output.strip().split("\n")[-5:]
            return instance_id, False, "\n".join(last_lines)

    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", container_name], capture_output=True)
        return instance_id, False, "timeout"
    except Exception as e:
        return instance_id, False, str(e)


# Evaluate all predictions
results = {}
passed = 0
failed = 0
errors = 0

print(f"Evaluating {len(preds)} patches with {max_workers} workers...")

with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = {}
    for iid, pred in preds.items():
        patch = pred.get("model_patch", "") if isinstance(pred, dict) else ""
        futures[executor.submit(eval_instance, iid, patch)] = iid

    for future in as_completed(futures):
        iid, ok, error = future.result()
        results[iid] = {"passed": ok, "error": error}
        if ok:
            passed += 1
            print(f"  PASS: {iid}")
        elif error in ("no dockerhub_tag", "no run_script.sh", "empty patch"):
            errors += 1
        else:
            failed += 1

total = passed + failed + errors
print(f"\n{'='*60}")
print(f"RESULTS: {passed}/{total} passed ({100*passed/max(total,1):.1f}%)")
print(f"  Passed:  {passed}")
print(f"  Failed:  {failed}")
print(f"  Errors:  {errors} (no tag/script/patch)")
print(f"{'='*60}")

# Save results
results_path = os.path.join(output_dir, "results.json")
with open(results_path, "w") as f:
    json.dump({
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "accuracy": passed / max(total, 1),
        "passed_ids": [iid for iid, r in results.items() if r["passed"]],
        "failed_ids": [iid for iid, r in results.items() if not r["passed"] and r["error"] not in ("no dockerhub_tag", "no run_script.sh", "empty patch")],
        "per_instance": results,
    }, f, indent=2)
print(f"Results saved to {results_path}")
