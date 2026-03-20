#!/usr/bin/env python3
"""Post-process existing baseline predictions with runtime introspection KB.

Takes baseline predictions, applies GT autocorrect with runtime KB to a copy,
saves both for evaluation. Zero API cost — same patches, one set corrected.
"""
import base64
import json
import os
import subprocess
import sys
import time


BASELINE_PREDS = os.path.expanduser("~/baseline_v42/preds.json")
WORK_DIR = os.path.expanduser("~/phase2b_postprocess_experiment")
GROUNDTRUTH_DIR = os.path.expanduser("~/groundtruth")


def run_docker(container: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command in a Docker container."""
    return subprocess.run(
        ["docker", "exec", container, "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def main():
    os.makedirs(WORK_DIR, exist_ok=True)

    # Load baseline predictions
    with open(BASELINE_PREDS) as f:
        preds = json.load(f)
    print("Loaded %d predictions from baseline" % len(preds))

    # Save original copy (untouched)
    orig_path = os.path.join(WORK_DIR, "preds_original.json")
    with open(orig_path, "w") as f:
        json.dump(preds, f, indent=2)

    # Identify unique repos
    repos: dict[str, list[str]] = {}
    for instance_id in preds:
        parts = instance_id.split("__")
        repo_key = parts[0] + "/" + parts[1].rsplit("-", 1)[0] if len(parts) >= 2 else instance_id
        repos.setdefault(repo_key, []).append(instance_id)

    print("\nUnique repos: %d" % len(repos))
    for repo, tasks in sorted(repos.items()):
        print("  %s: %d tasks" % (repo, len(tasks)))

    # Encode scripts as base64
    autocorrect_path = os.path.join(GROUNDTRUTH_DIR, "benchmarks/swebench/gt_autocorrect.py")
    runtime_kb_path = os.path.join(GROUNDTRUTH_DIR, "benchmarks/swebench/gt_runtime_kb.py")

    with open(autocorrect_path, "rb") as f:
        autocorrect_b64 = base64.b64encode(f.read()).decode("ascii")
    with open(runtime_kb_path, "rb") as f:
        runtime_kb_b64 = base64.b64encode(f.read()).decode("ascii")

    # Find tasks with non-empty patches
    tasks_to_process = []
    for instance_id, pred in preds.items():
        patch = pred.get("model_patch", "")
        if patch and patch.strip():
            tasks_to_process.append(instance_id)

    print("\nTasks with patches: %d" % len(tasks_to_process))
    print("Tasks without patches: %d" % (len(preds) - len(tasks_to_process)))

    # Process each task
    postprocessed_preds = dict(preds)
    report = {
        "total_tasks": len(preds),
        "tasks_with_patches": len(tasks_to_process),
        "tasks_processed": 0,
        "tasks_with_corrections": 0,
        "tasks_skipped_no_patch": len(preds) - len(tasks_to_process),
        "tasks_skipped_container_fail": 0,
        "corrections_by_type": {},
        "all_corrections": [],
        "per_task": {},
    }

    start_time = time.time()

    for idx, instance_id in enumerate(sorted(tasks_to_process)):
        if idx > 0 and idx % 10 == 0:
            elapsed = time.time() - start_time
            rate = idx / elapsed * 60 if elapsed > 0 else 0
            print("[%d/%d] %.0f tasks/min | corrections: %d | container_fail: %d" % (
                idx, len(tasks_to_process), rate,
                report["tasks_with_corrections"],
                report["tasks_skipped_container_fail"]))

        pred = preds[instance_id]
        original_patch = pred.get("model_patch", "")
        container_name = "gt_pp_%d" % idx

        task_report = {
            "instance_id": instance_id,
            "container_ok": False,
            "kb_classes": 0,
            "autocorrect_ran": False,
            "corrections": [],
            "error": None,
        }

        try:
            # Find Docker image for this task
            # Image naming: swebench/sweb.eval.x86_64.<org>_<num>_<repo>-<issue>:latest
            # instance_id: django__django-12856 -> search for "django-12856"
            parts = instance_id.split("__")
            search_term = parts[1] if len(parts) >= 2 else instance_id
            img_result = subprocess.run(
                ["bash", "-c",
                 "docker images --format '{{.Repository}}:{{.Tag}}' | grep '%s' | head -1" % search_term],
                capture_output=True, text=True, timeout=10,
            )
            image = img_result.stdout.strip()

            if not image:
                task_report["error"] = "no_docker_image"
                report["tasks_skipped_container_fail"] += 1
                report["per_task"][instance_id] = task_report
                continue

            # Start container
            start_result = subprocess.run(
                ["docker", "run", "-d", "--name", container_name,
                 "-w", "/testbed", "--rm", image, "sleep", "300"],
                capture_output=True, text=True, timeout=60,
            )
            if start_result.returncode != 0:
                task_report["error"] = "container_start_failed: " + start_result.stderr[:100]
                report["tasks_skipped_container_fail"] += 1
                report["per_task"][instance_id] = task_report
                continue

            task_report["container_ok"] = True

            # Copy scripts via base64
            run_docker(container_name,
                       "echo '%s' | base64 -d > /tmp/gt_autocorrect.py" % autocorrect_b64, timeout=10)
            run_docker(container_name,
                       "echo '%s' | base64 -d > /tmp/gt_runtime_kb.py" % runtime_kb_b64, timeout=10)

            # Build runtime KB
            kb_result = run_docker(container_name,
                                   "cd /testbed && python3 /tmp/gt_runtime_kb.py 2>/dev/null",
                                   timeout=30)
            try:
                kb_data = json.loads(kb_result.stdout)
                task_report["kb_classes"] = kb_data.get("total_classes", 0)
                # Save KB in container
                kb_b64 = base64.b64encode(kb_result.stdout.encode()).decode("ascii")
                run_docker(container_name,
                           "echo '%s' | base64 -d > /tmp/gt_runtime_kb.json" % kb_b64,
                           timeout=10)
            except (json.JSONDecodeError, ValueError):
                pass  # KB build failed — autocorrect will use AST KB only

            # Apply the baseline patch
            patch_b64 = base64.b64encode(original_patch.encode()).decode("ascii")
            run_docker(container_name,
                       "echo '%s' | base64 -d > /tmp/patch.diff" % patch_b64, timeout=10)
            apply_result = run_docker(container_name,
                                      "cd /testbed && git apply /tmp/patch.diff 2>&1",
                                      timeout=15)
            if apply_result.returncode != 0:
                # Try with --reject for partial application
                run_docker(container_name,
                           "cd /testbed && git apply --reject /tmp/patch.diff 2>/dev/null; true",
                           timeout=15)

            # Run autocorrect
            ac_result = run_docker(container_name,
                                   "cd /testbed && python3 /tmp/gt_autocorrect.py 2>/dev/null",
                                   timeout=30)
            try:
                ac_data = json.loads(ac_result.stdout)
                task_report["autocorrect_ran"] = True
                corrections = ac_data.get("corrections", [])
                task_report["corrections"] = corrections

                if corrections:
                    # Extract corrected diff
                    diff_result = run_docker(container_name, "cd /testbed && git diff", timeout=15)
                    corrected_patch = diff_result.stdout
                    if corrected_patch.strip():
                        postprocessed_preds[instance_id] = dict(pred)
                        postprocessed_preds[instance_id]["model_patch"] = corrected_patch
                        report["tasks_with_corrections"] += 1

                        for c in corrections:
                            ct = c.get("check_type", "unknown")
                            report["corrections_by_type"][ct] = report["corrections_by_type"].get(ct, 0) + 1
                            report["all_corrections"].append({
                                "instance_id": instance_id,
                                "old_name": c.get("old_name"),
                                "new_name": c.get("new_name"),
                                "check_type": ct,
                                "reason": c.get("reason"),
                            })
            except (json.JSONDecodeError, ValueError):
                task_report["error"] = "autocorrect_json_fail"

            report["tasks_processed"] += 1

        except subprocess.TimeoutExpired:
            task_report["error"] = "timeout"
            report["tasks_skipped_container_fail"] += 1
        except Exception as e:
            task_report["error"] = str(e)[:200]
            report["tasks_skipped_container_fail"] += 1
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True, timeout=10,
            )

        report["per_task"][instance_id] = task_report

    # Save postprocessed predictions
    pp_path = os.path.join(WORK_DIR, "preds_postprocessed.json")
    with open(pp_path, "w") as f:
        json.dump(postprocessed_preds, f, indent=2)

    # Save report (without per_task for readability)
    summary_report = {k: v for k, v in report.items() if k != "per_task"}
    report_path = os.path.join(WORK_DIR, "postprocessing_report.json")
    with open(report_path, "w") as f:
        json.dump(summary_report, f, indent=2, default=str)

    # Save full report
    full_report_path = os.path.join(WORK_DIR, "postprocessing_report_full.json")
    with open(full_report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Print summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("POST-PROCESSING COMPLETE")
    print("=" * 60)
    print("Total tasks: %d" % report["total_tasks"])
    print("Tasks processed: %d" % report["tasks_processed"])
    print("Tasks with corrections: %d" % report["tasks_with_corrections"])
    print("Skipped (no patch): %d" % report["tasks_skipped_no_patch"])
    print("Skipped (container fail): %d" % report["tasks_skipped_container_fail"])
    print("Corrections by type: %s" % report["corrections_by_type"])
    print("Time: %.1f minutes" % (elapsed / 60))
    print()
    if report["all_corrections"]:
        print("All corrections:")
        for c in report["all_corrections"]:
            print("  %s: %s -> %s (%s) | %s" % (
                c["instance_id"], c["old_name"], c["new_name"],
                c["check_type"], c["reason"]))
    else:
        print("No corrections applied (all patches were clean)")
    print()
    print("Files saved:")
    print("  Original: %s" % orig_path)
    print("  Post-processed: %s" % pp_path)
    print("  Report: %s" % report_path)


if __name__ == "__main__":
    main()
