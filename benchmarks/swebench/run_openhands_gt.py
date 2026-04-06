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
        # Live Lite: starryzhang namespace, same _1776_ pattern
        id_docker = iid.replace("__", "_1776_")
        return f"docker.io/starryzhang/sweb.eval.x86_64.{id_docker}:latest".lower()
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


def _exec_in_container(container_id: str, cmd: str, timeout: int = 60) -> str:
    """Execute a bash command in a Docker container and return output."""
    result = subprocess.run(
        ["docker", "exec", container_id, "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    output = result.stdout
    if result.returncode != 0 and result.stderr:
        output += f"\n[stderr]: {result.stderr[-500:]}"
    return output[:10000]  # Cap output size


def _run_agent_loop(
    container_id: str,
    repo_root: str,
    task_text: str,
    model: str,
    task_dir: Path,
    instance_id: str,
    max_iterations: int = 100,
) -> str:
    """Minimal agent loop: LLM generates bash commands, we execute in container.

    Returns the git diff patch (or empty string).
    """
    import requests

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    base_url = "https://openrouter.ai/api/v1"

    system_prompt = f"""You are a software engineer fixing a bug in a repository at {repo_root}.
You have access to a bash tool to run commands. Use it to explore the code, understand the issue, make changes, and verify your fix.

IMPORTANT:
- Only modify source files, NOT test files
- When done, call the submit tool with your git diff
- Be concise and efficient — minimize exploration, focus on the fix"""

    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a bash command in the repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The bash command to run"}
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "submit",
                "description": "Submit your fix as a git diff. Call this when done.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Brief description of the fix"}
                    },
                    "required": ["message"]
                }
            }
        },
    ]

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_text},
    ]

    total_cost = 0.0
    for step in range(max_iterations):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "temperature": 0.7,
                    "top_p": 0.8,
                    "max_tokens": 4096,
                },
                timeout=120,
            )
            if resp.status_code != 200:
                logger.error("LLM API error on step %d: %s", step, resp.text[:200])
                break

            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
            messages.append(msg)

            # Track cost
            usage = data.get("usage", {})
            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                total_cost += input_tokens * 0.22 / 1_000_000 + output_tokens * 1.0 / 1_000_000

            # Check for tool calls
            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                # No tool call — model sent a text message; nudge it
                if choice.get("finish_reason") == "stop":
                    # Model thinks it's done but didn't submit
                    messages.append({"role": "user", "content": "Please use the submit tool to submit your fix, or use the bash tool to continue working."})
                continue

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name == "bash":
                    cmd = fn_args.get("command", "echo 'no command'")
                    logger.debug("[%s] step %d: bash: %s", instance_id, step, cmd[:100])
                    output = _exec_in_container(container_id, cmd)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": output,
                    })

                elif fn_name == "submit":
                    logger.info("[%s] Agent submitted at step %d (cost: $%.2f)", instance_id, step, total_cost)
                    # Get the diff
                    diff = _exec_in_container(container_id, f"cd {repo_root} && git diff", timeout=15)
                    if diff.strip():
                        (task_dir / "patch.diff").write_text(diff)
                        logger.info("[%s] Patch: %d chars", instance_id, len(diff))
                        return diff
                    else:
                        # Try git diff HEAD
                        diff = _exec_in_container(container_id, f"cd {repo_root} && git diff HEAD", timeout=15)
                        if diff.strip():
                            (task_dir / "patch.diff").write_text(diff)
                            return diff
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "No changes detected. Did you forget to edit files?",
                    })

        except requests.Timeout:
            logger.warning("[%s] LLM timeout at step %d", instance_id, step)
            continue
        except Exception as e:
            logger.error("[%s] Error at step %d: %s", instance_id, step, e)
            break

    # Agent didn't submit — try to get diff anyway
    logger.warning("[%s] Agent exhausted %d iterations (cost: $%.2f)", instance_id, max_iterations, total_cost)
    diff = _exec_in_container(container_id, f"cd {repo_root} && git diff", timeout=15)
    if diff.strip():
        (task_dir / "patch.diff").write_text(diff)
        return diff
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

        # Step 3: Start a fresh container from the SWE-bench image for the agent
        agent_container = f"oh-agent-{iid.replace('__', '-')[:40]}"
        subprocess.run(["docker", "kill", agent_container], capture_output=True)
        subprocess.run(["docker", "rm", agent_container], capture_output=True)

        agent_start = subprocess.run(
            ["docker", "run", "-d", "--name", agent_container,
             "-w", repo_root, "--entrypoint", "",
             image, "sleep", "30m"],
            capture_output=True, text=True, timeout=120,
        )
        agent_cid = agent_start.stdout.strip()
        if not agent_cid:
            logger.error("Could not start agent container for %s: %s", iid, agent_start.stderr[:200])
            return {"instance_id": iid, "model_patch": None, "error": "container_start_failed"}

        # Step 4: Run agent loop — LLM + bash execution in container
        patch = _run_agent_loop(agent_cid, repo_root, enhanced_task, model, task_dir, iid)

        # Save results
        result_data = {
            "instance_id": iid,
            "model_patch": patch if patch else None,
            "gt_briefing": gt_briefing[:500] if gt_briefing else None,
        }

        (task_dir / "result.json").write_text(json.dumps(result_data, indent=2))

        logger.info("Completed: %s (patch: %s)", iid, "YES" if patch else "NO")
        return result_data

    except subprocess.TimeoutExpired:
        logger.error("Timeout on %s", iid)
        return {"instance_id": iid, "model_patch": None, "error": "timeout"}
    except Exception as e:
        logger.error("Error on %s: %s", iid, e)
        return {"instance_id": iid, "model_patch": None, "error": str(e)}
    finally:
        # Cleanup containers
        subprocess.run(["docker", "kill", container_name], capture_output=True)
        subprocess.run(["docker", "rm", container_name], capture_output=True)
        if 'agent_container' in dir():
            subprocess.run(["docker", "kill", agent_container], capture_output=True)
            subprocess.run(["docker", "rm", agent_container], capture_output=True)


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
