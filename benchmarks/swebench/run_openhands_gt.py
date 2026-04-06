#!/usr/bin/env python3
"""Run OpenHands + GT on SWE-bench tasks.

Batch harness that:
1. Loads SWE-bench tasks from HuggingFace
2. For each task: starts Docker container, runs gt-index, gets GT briefing
3. Injects GT briefing into task prompt
4. Calls OpenHands CLI to solve the task
5. Extracts patch from the session output

Usage:
    python3 run_openhands_gt.py \
        --subset princeton-nlp/SWE-bench_Verified --split test \
        --filter 'django__django-11066|sympy__sympy-15976' \
        --model openrouter/qwen/qwen3-coder \
        --workers 2 \
        --output ~/track3_results/
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("openhands-gt")

# GT binary and scripts
GT_INDEX_BINARY = Path(__file__).parent.parent.parent / "gt-index" / "gt-index-static"
GT_INTEL_SCRIPT = Path(__file__).parent / "gt_intel.py"
GT_EGO_GRAPH = Path(__file__).parent.parent.parent / "src" / "groundtruth" / "ego_graph.py"


def load_tasks(subset: str, split: str, filter_regex: str | None = None) -> list[dict]:
    """Load SWE-bench tasks from HuggingFace."""
    from datasets import load_dataset
    ds = load_dataset(subset, split=split)
    tasks = list(ds)
    if filter_regex:
        pattern = re.compile(filter_regex)
        tasks = [t for t in tasks if pattern.search(t["instance_id"])]
    logger.info("Loaded %d tasks from %s/%s", len(tasks), subset, split)
    return tasks


def get_docker_image(task: dict, dataset: str = "") -> str:
    """Get the Docker image name for a SWE-bench task.

    SWE-bench Verified: swebench/sweb.eval.x86_64.{owner}_1776_{repo-issue}:latest
    SWE-bench Live:     sweb.eval.x86_64.{instance_id}:latest
    """
    iid = task["instance_id"]
    if "Live" in dataset or "live" in dataset:
        # Live Lite: image tag is just the instance_id
        return f"sweb.eval.x86_64.{iid}:latest"
    # Verified/Lite: legacy pattern with version tag
    parts = iid.split("__")  # ['django', 'django-11066']
    owner = parts[0]
    rest = parts[1] if len(parts) > 1 else iid
    return f"docker.io/swebench/sweb.eval.x86_64.{owner}_1776_{rest}:latest"


def run_gt_index(container_id: str, repo_root: str) -> bool:
    """Run gt-index inside the container."""
    if not GT_INDEX_BINARY.exists():
        logger.warning("gt-index binary not found at %s", GT_INDEX_BINARY)
        return False
    try:
        subprocess.run(
            ["docker", "cp", str(GT_INDEX_BINARY), f"{container_id}:/tmp/gt-index"],
            timeout=15, check=True, capture_output=True,
        )
        subprocess.run(
            ["docker", "exec", container_id, "chmod", "+x", "/tmp/gt-index"],
            timeout=5, check=True, capture_output=True,
        )
        result = subprocess.run(
            ["docker", "exec", container_id,
             "/tmp/gt-index", f"--root={repo_root}", "--output=/tmp/gt_graph.db", "--max-files=5000"],
            timeout=30, capture_output=True, text=True,
        )
        last_line = result.stdout.strip().split("\n")[-1][:120] if result.stdout else "no output"
        logger.info("gt-index: %s", last_line)
        return True
    except Exception as e:
        logger.warning("gt-index failed: %s", e)
        return False


def get_gt_briefing(container_id: str, repo_root: str, issue_text: str) -> str:
    """Get GT briefing for a task."""
    try:
        # Copy GT scripts
        subprocess.run(
            ["docker", "cp", str(GT_INTEL_SCRIPT), f"{container_id}:/tmp/gt_intel.py"],
            timeout=10, check=True, capture_output=True,
        )
        if GT_EGO_GRAPH.exists():
            subprocess.run(
                ["docker", "cp", str(GT_EGO_GRAPH), f"{container_id}:/tmp/ego_graph.py"],
                timeout=10, check=True, capture_output=True,
            )

        # Write issue text to file in container
        escaped = issue_text.replace("'", "'\\''")[:5000]  # Limit length
        subprocess.run(
            ["docker", "exec", container_id, "bash", "-c",
             f"echo '{escaped}' > /tmp/issue.txt"],
            timeout=10, capture_output=True,
        )

        # Run gt_intel.py --enhanced-briefing
        result = subprocess.run(
            ["docker", "exec", container_id, "python3",
             "/tmp/gt_intel.py", "--db=/tmp/gt_graph.db",
             "--enhanced-briefing", "--issue-text=@/tmp/issue.txt",
             f"--root={repo_root}"],
            timeout=15, capture_output=True, text=True,
        )
        output = result.stdout.strip()
        if output and len(output) > 10 and "Error" not in output[:30]:
            logger.info("GT briefing: %d chars", len(output))
            return output
    except Exception as e:
        logger.warning("GT briefing failed: %s", e)
    return ""


def solve_task(task: dict, output_dir: Path, model: str, config_path: str, dataset: str = "") -> dict:
    """Solve a single SWE-bench task with OpenHands + GT."""
    iid = task["instance_id"]
    logger.info("Starting: %s", iid)

    task_dir = output_dir / iid
    task_dir.mkdir(parents=True, exist_ok=True)

    image = task.get("docker_image") or get_docker_image(task, dataset=dataset)
    repo_root = "/testbed"

    try:
        # Step 1: Pre-index with GT using a temporary container
        gt_briefing = ""
        container_name = f"gt-idx-{iid.replace('__', '-')[:40]}"
        subprocess.run(["docker", "kill", container_name], capture_output=True)
        subprocess.run(["docker", "rm", container_name], capture_output=True)

        idx_result = subprocess.run(
            ["docker", "run", "-d", "--name", container_name,
             "-w", repo_root, "--entrypoint", "",
             image, "sleep", "5m"],
            capture_output=True, text=True, timeout=120,
        )
        container_id = idx_result.stdout.strip()
        if container_id:
            logger.info("GT index container: %s", container_id[:12])
            if run_gt_index(container_id, repo_root):
                gt_briefing = get_gt_briefing(container_id, repo_root, task["problem_statement"])
            subprocess.run(["docker", "kill", container_name], capture_output=True)
        else:
            logger.warning("Could not start GT index container: %s", idx_result.stderr[:200])

        # Step 2: Build enhanced task with GT context
        issue_text = task["problem_statement"]
        if gt_briefing:
            enhanced_task = f"{gt_briefing}\n\n{issue_text}"
        else:
            enhanced_task = issue_text

        enhanced_task += f"""

Fix the issue described above by modifying the source code in the repository.
The repository is checked out at {repo_root}.
When done, create a git diff of ONLY the source files you changed (no test files):
  git diff -- path/to/file1 path/to/file2 > /tmp/patch.diff
Do NOT modify test files, configuration files, or setup scripts.
"""

        # Write task to file for OpenHands
        task_file = task_dir / "task.txt"
        task_file.write_text(enhanced_task)

        # Step 3: Write per-task config with SWE-bench image as base
        task_config = task_dir / "config.toml"
        task_config.write_text(f"""[core]
workspace_base = "{task_dir}/workspace"
max_iterations = 100

[sandbox]
base_container_image = "{image}"
timeout = 120
use_host_network = true

[llm]
model = "{model}"
api_key = "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"
base_url = "https://openrouter.ai/api/v1"
temperature = 0.7
top_p = 0.8
""")

        # Step 4: Run OpenHands CLI with per-task config
        oh_result = subprocess.run(
            ["openhands", "cli",
             "--config-file", str(task_config),
             "-f", str(task_file),
             "--name", iid],
            capture_output=True, text=True,
            timeout=900,  # 15 min per task
        )

        # Step 5: Extract patch from OpenHands output or workspace
        patch = ""
        # Check if OpenHands left a patch in the output
        if oh_result.stdout:
            # Look for diff output in stdout
            lines = oh_result.stdout.split("\n")
            in_diff = False
            diff_lines = []
            for line in lines:
                if line.startswith("diff --git"):
                    in_diff = True
                if in_diff:
                    diff_lines.append(line)
                    if line == "" and diff_lines:
                        in_diff = False
            if diff_lines:
                patch = "\n".join(diff_lines)

        # Save results
        result_data = {
            "instance_id": iid,
            "model_patch": patch if patch else None,
            "gt_briefing": gt_briefing[:500] if gt_briefing else None,
            "oh_stdout_tail": oh_result.stdout[-1000:] if oh_result.stdout else "",
            "oh_stderr_tail": oh_result.stderr[-500:] if oh_result.stderr else "",
        }

        (task_dir / "result.json").write_text(json.dumps(result_data, indent=2))
        if patch:
            (task_dir / "patch.diff").write_text(patch)

        logger.info("Completed: %s (patch: %s)", iid, "YES" if patch else "NO")
        return result_data

    except subprocess.TimeoutExpired:
        logger.error("Timeout on %s", iid)
        return {"instance_id": iid, "model_patch": None, "error": "timeout"}
    except Exception as e:
        logger.error("Error on %s: %s", iid, e)
        return {"instance_id": iid, "model_patch": None, "error": str(e)}
    finally:
        # Cleanup container
        subprocess.run(["docker", "kill", container_name], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description="Run OpenHands + GT on SWE-bench")
    parser.add_argument("--subset", default="princeton-nlp/SWE-bench_Verified")
    parser.add_argument("--split", default="test")
    parser.add_argument("--filter", help="Regex filter on instance IDs")
    parser.add_argument("--model", default="openrouter/qwen/qwen3-coder")
    parser.add_argument("--config", default=os.path.expanduser("~/openhands_config.toml"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", "-o", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = load_tasks(args.subset, args.split, args.filter)
    logger.info("Running %d tasks with %d workers", len(tasks), args.workers)

    # Update config with model
    # TODO: dynamically update config.toml with model name

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(solve_task, task, output_dir, args.model, args.config, dataset=args.subset): task
            for task in tasks
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            # Write incremental predictions
            preds = {r["instance_id"]: r for r in results}
            (output_dir / "preds.json").write_text(json.dumps(preds, indent=2))

    # Summary
    submitted = sum(1 for r in results if r.get("model_patch"))
    logger.info("Done. %d/%d patches submitted.", submitted, len(results))


if __name__ == "__main__":
    main()
