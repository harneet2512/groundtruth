#!/usr/bin/env python3
"""Evaluate SWE-bench Pro predictions — apply model patch + test patch, run tests."""
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datasets import load_dataset

preds_path = sys.argv[1]
output_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/eval_v12"
max_workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
os.makedirs(output_dir, exist_ok=True)

preds = json.load(open(preds_path))
print(f"Loaded {len(preds)} predictions")

ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
instance_map = {row["instance_id"]: row for row in ds}
print(f"Dataset: {len(instance_map)} instances")


def eval_instance(instance_id, pred_data):
    patch = pred_data.get("model_patch", "") if isinstance(pred_data, dict) else str(pred_data)
    if not patch or len(patch.strip()) < 10:
        return instance_id, False, "empty patch"

    info = instance_map.get(instance_id)
    if not info:
        return instance_id, False, "not in dataset"

    tag = info.get("dockerhub_tag", "")
    if not tag:
        return instance_id, False, "no dockerhub_tag"

    image = f"jefzda/sweap-images:{tag}"
    test_patch = info.get("test_patch", "")
    test_files = info.get("selected_test_files_to_run", [])
    repo_lang = info.get("repo_language", "python")
    before_cmd = info.get("before_repo_set_cmd", "")

    container = f"eval-{instance_id[:40].replace('/', '-')}-{os.getpid()}"

    try:
        # Start container
        subprocess.run(
            ["docker", "run", "-d", "--name", container, "-w", "/app",
             "--rm", "--entrypoint", "", image, "sleep", "300"],
            capture_output=True, timeout=120, check=True,
        )

        # Apply model patch
        patch_file = tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False)
        patch_file.write(patch)
        patch_file.close()
        subprocess.run(
            ["docker", "cp", patch_file.name, f"{container}:/tmp/model.patch"],
            capture_output=True, timeout=10,
        )
        r = subprocess.run(
            ["docker", "exec", container, "bash", "-c",
             "cd /app && git apply /tmp/model.patch 2>&1 || git apply --3way /tmp/model.patch 2>&1"],
            capture_output=True, text=True, timeout=30,
        )
        os.unlink(patch_file.name)
        if r.returncode != 0:
            return instance_id, False, f"patch failed: {r.stderr[:200]}"

        # Apply test patch
        if test_patch:
            tp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False)
            tp_file.write(test_patch)
            tp_file.close()
            subprocess.run(
                ["docker", "cp", tp_file.name, f"{container}:/tmp/test.patch"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["docker", "exec", container, "bash", "-c",
                 "cd /app && git apply /tmp/test.patch 2>&1 || git apply --3way /tmp/test.patch 2>&1"],
                capture_output=True, text=True, timeout=30,
            )
            os.unlink(tp_file.name)

        # Run before_repo_set_cmd if exists
        if before_cmd:
            subprocess.run(
                ["docker", "exec", container, "bash", "-c", f"cd /app && {before_cmd}"],
                capture_output=True, text=True, timeout=120,
            )

        # Run tests
        if test_files:
            tf = " ".join(test_files)
            if repo_lang == "python":
                test_cmd = f"cd /app && python -m pytest {tf} -x --timeout=60 2>&1"
            elif repo_lang in ("javascript", "typescript"):
                test_cmd = f"cd /app && npx jest {tf} --forceExit 2>&1 || npx mocha {tf} 2>&1"
            elif repo_lang == "go":
                test_cmd = f"cd /app && go test {tf} -timeout 60s 2>&1"
            else:
                test_cmd = f"cd /app && python -m pytest {tf} -x --timeout=60 2>&1"

            r = subprocess.run(
                ["docker", "exec", container, "bash", "-c", test_cmd],
                capture_output=True, text=True, timeout=180,
            )
            passed = r.returncode == 0
            return instance_id, passed, r.stdout[-500:] if not passed else "passed"
        else:
            return instance_id, False, "no test files"

    except subprocess.TimeoutExpired:
        return instance_id, False, "timeout"
    except Exception as e:
        return instance_id, False, str(e)[:200]
    finally:
        subprocess.run(["docker", "kill", container], capture_output=True, timeout=10)
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)


results = {}
n_passed = 0
n_failed = 0
n_errors = 0

with ThreadPoolExecutor(max_workers=max_workers) as pool:
    futures = {}
    for k, v in preds.items():
        iid = v.get("instance_id", k) if isinstance(v, dict) else k
        futures[pool.submit(eval_instance, iid, v)] = iid

    for i, f in enumerate(as_completed(futures)):
        iid, ok, msg = f.result()
        results[iid] = {"passed": ok, "message": msg[:300]}
        if ok:
            n_passed += 1
        elif "empty" in msg or "not in" in msg or "no " in msg:
            n_errors += 1
        else:
            n_failed += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{i+1}/{len(preds)}] {iid[:60]}: {status}")

print(f"\n{'='*60}")
print(f"RESULTS: {n_passed}/{len(preds)} passed ({100*n_passed/max(len(preds),1):.1f}%)")
print(f"  Passed: {n_passed}  Failed: {n_failed}  Errors: {n_errors}")
print(f"{'='*60}")

json.dump(results, open(f"{output_dir}/results.json", "w"), indent=2)
print(f"Saved to {output_dir}/results.json")
