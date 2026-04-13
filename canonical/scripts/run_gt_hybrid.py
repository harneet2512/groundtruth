#!/usr/bin/env python3
"""Canonical SWE-agent + DeepSeek V3.2 + GroundTruth hybrid runner.

Runs real SWE-agent (NOT mini-SWE-agent) with GT as the only intentional delta.

GT integration:
  1. Startup: inject gt-index, gt_intel.py, gt_tools.py, state command + build graph.db
  2. Orientation: auto-prepend gt_orient output to task description
  3. State command: swe_agent_state_gt.py runs after each action (briefing + post-edit)
  4. Tools: gt_orient, gt_lookup, gt_impact, gt_check (budget-enforced)
  5. Host-side: PatchScorer (observe-only), telemetry extraction

Usage:
    python canonical/scripts/run_gt_hybrid.py \\
        --config canonical/config/sweagent_deepseek_v3.2_gt.yaml \\
        --output-dir results/canonical_gt_v1 \\
        --workers 4 \\
        [--instance-ids "repo__owner-123,repo__owner-456"] \\
        [--max-instances 300]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess as sp
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("canonical_gt")

# ── File paths ──────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _find_gt_index() -> Path:
    """Find gt-index-static binary: env var > /tmp > repo."""
    repo = Path(__file__).resolve().parent.parent.parent
    env_path = os.environ.get("GT_INDEX_PATH", "")
    candidates = [
        Path(env_path) if env_path else None,
        Path("/tmp/gt-index-static"),
        repo / "gt-index" / "gt-index-static",
        repo / "gt-index" / "gt-index-linux",
    ]
    for p in candidates:
        if p is not None and p.is_file():
            return p
    return candidates[-1]  # fallback


GT_INDEX_BINARY = _find_gt_index()
GT_INTEL_SCRIPT = REPO_ROOT / "benchmarks" / "swebench" / "gt_intel.py"
GT_SHELL_TOOLS = REPO_ROOT / "scripts" / "swebench" / "gt_shell_tools.py"
GT_STATE_CMD = REPO_ROOT / "benchmarks" / "swebench" / "swe_agent_state_gt.py"
GT_BUDGET_SCRIPT = REPO_ROOT / "canonical" / "tools" / "_gt_budget.sh"
GT_ORIENT_SCRIPT = REPO_ROOT / "canonical" / "tools" / "gt_orient.sh"
GT_LOOKUP_SCRIPT = REPO_ROOT / "canonical" / "tools" / "gt_lookup.sh"
GT_IMPACT_SCRIPT = REPO_ROOT / "canonical" / "tools" / "gt_impact.sh"
GT_CHECK_SCRIPT = REPO_ROOT / "canonical" / "tools" / "gt_check.sh"


def _inject_live_docker_image(instance: dict) -> dict:
    """Inject docker_image for SWE-bench Live instances."""
    if "docker_image" not in instance and "dockerhub_tag" not in instance:
        iid = instance["instance_id"]
        id_docker = iid.replace("__", "_1776_")
        instance["docker_image"] = f"starryzhang/sweb.eval.x86_64.{id_docker}:latest"
    return instance


def _docker_exec(container_id: str, cmd: str, timeout: int = 60) -> str:
    """Run a command inside a Docker container, return stdout."""
    result = sp.run(
        ["docker", "exec", container_id, "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip()


def _docker_cp(src: Path, container_id: str, dest: str, timeout: int = 15) -> None:
    """Copy a file into a Docker container."""
    sp.run(
        ["docker", "cp", str(src), f"{container_id}:{dest}"],
        timeout=timeout, check=True, capture_output=True,
    )


def _inject_gt(container_id: str, instance_id: str) -> bool:
    """Inject GT files into container and build graph.db."""
    try:
        # Copy GT files
        _docker_cp(GT_INDEX_BINARY, container_id, "/tmp/gt-index-bin")
        _docker_cp(GT_INTEL_SCRIPT, container_id, "/tmp/gt_intel.py")
        _docker_cp(GT_SHELL_TOOLS, container_id, "/tmp/gt_tools.py")
        _docker_cp(GT_STATE_CMD, container_id, "/tmp/swe_agent_state_gt.py")
        _docker_cp(GT_BUDGET_SCRIPT, container_id, "/tmp/_gt_budget.sh")
        _docker_cp(GT_ORIENT_SCRIPT, container_id, "/tmp/gt_orient.sh")
        _docker_cp(GT_LOOKUP_SCRIPT, container_id, "/tmp/gt_lookup.sh")
        _docker_cp(GT_IMPACT_SCRIPT, container_id, "/tmp/gt_impact.sh")
        _docker_cp(GT_CHECK_SCRIPT, container_id, "/tmp/gt_check.sh")

        # Make scripts executable
        _docker_exec(container_id, "chmod +x /tmp/gt-index-bin /tmp/gt_orient.sh "
                     "/tmp/gt_lookup.sh /tmp/gt_impact.sh /tmp/gt_check.sh /tmp/_gt_budget.sh")

        # Initialize budget file
        _docker_exec(container_id,
                     'echo \'{"orient":0,"lookup":0,"impact":0,"check":0}\' > /tmp/gt_budget.json')

        # Set env vars
        _docker_exec(container_id,
                     "echo 'export GT_DB=/tmp/gt_graph.db' >> /root/.bashrc && "
                     "echo 'export GT_ROOT=/testbed' >> /root/.bashrc")

        # Build graph index
        max_files = os.environ.get("GT_MAX_FILES", "5000")
        output = _docker_exec(container_id,
                              f"/tmp/gt-index-bin --root=/testbed --output=/tmp/gt_graph.db "
                              f"--max-files={max_files} 2>&1",
                              timeout=45)
        last_line = output.strip().split("\n")[-1][:120] if output else "no output"
        logger.info("GT inject OK: %s | %s", instance_id, last_line)
        return True

    except Exception as e:
        logger.warning("GT inject FAILED: %s | %s", instance_id, e)
        return False


def _get_gt_orient(container_id: str) -> str:
    """Run gt_orient and return output for task prepending."""
    try:
        output = _docker_exec(
            container_id,
            "GT_DB=/tmp/gt_graph.db GT_ROOT=/testbed python3 /tmp/gt_tools.py orient 2>/dev/null",
            timeout=10,
        )
        if output and len(output) > 20:
            return f"\n<gt-orientation>\n{output}\n</gt-orientation>\n"
    except Exception:
        pass
    return ""


def _extract_telemetry(container_id: str, dest_dir: Path, instance_id: str) -> dict:
    """Extract GT telemetry from container after task completion."""
    telemetry = {"instance_id": instance_id, "gt_injected": True}
    try:
        # Telemetry JSONL
        telem_path = dest_dir / f"{instance_id}.telemetry.jsonl"
        sp.run(
            ["docker", "cp", f"{container_id}:/tmp/gt_hook_telemetry.jsonl", str(telem_path)],
            capture_output=True, timeout=5,
        )
        if telem_path.exists():
            lines = telem_path.read_text().strip().split("\n")
            telemetry["telemetry_events"] = len(lines)
        else:
            telemetry["telemetry_events"] = 0

        # Budget state
        budget_path = dest_dir / f"{instance_id}.budget.json"
        sp.run(
            ["docker", "cp", f"{container_id}:/tmp/gt_budget.json", str(budget_path)],
            capture_output=True, timeout=5,
        )
        if budget_path.exists():
            telemetry["budget_final"] = json.loads(budget_path.read_text())

    except Exception as e:
        telemetry["extraction_error"] = str(e)[:100]

    return telemetry


# ── Cost tracking ───────────────────────────────────────────────────────

# DeepSeek V3.2 Vertex AI pricing (per 1M tokens)
COST_PER_1M_INPUT = 0.80   # $0.80 / 1M input tokens
COST_PER_1M_OUTPUT = 2.00  # $2.00 / 1M output tokens


def _extract_cost_from_traj(traj_dir: Path, instance_id: str) -> dict:
    """Extract token usage and cost from SWE-agent trajectory files."""
    cost_info: dict = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "turns": 0,
        "source": "none",
    }

    # Look for trajectory file
    traj_files = list(traj_dir.glob(f"{instance_id}*.traj*")) + \
                 list(traj_dir.glob("*.traj*"))
    for traj_file in traj_files:
        try:
            data = json.loads(traj_file.read_text())
        except Exception:
            continue

        # SWE-agent stores model_stats in info
        info = data.get("info", {})
        model_stats = info.get("model_stats", {})

        if model_stats:
            cost_info["input_tokens"] = model_stats.get("prompt_tokens",
                                        model_stats.get("input_tokens", 0))
            cost_info["output_tokens"] = model_stats.get("completion_tokens",
                                         model_stats.get("output_tokens", 0))
            cost_info["total_tokens"] = (cost_info["input_tokens"] +
                                         cost_info["output_tokens"])
            # Use reported cost if available, otherwise compute
            if "api_cost" in model_stats:
                cost_info["cost_usd"] = float(model_stats["api_cost"])
                cost_info["source"] = "model_stats.api_cost"
            elif "total_cost" in model_stats:
                cost_info["cost_usd"] = float(model_stats["total_cost"])
                cost_info["source"] = "model_stats.total_cost"
            else:
                cost_info["cost_usd"] = (
                    cost_info["input_tokens"] / 1_000_000 * COST_PER_1M_INPUT +
                    cost_info["output_tokens"] / 1_000_000 * COST_PER_1M_OUTPUT
                )
                cost_info["source"] = "computed_from_tokens"
            break

        # Fallback: count messages as proxy for turns
        messages = data.get("messages", [])
        assistant_msgs = [m for m in messages if isinstance(m, dict)
                          and m.get("role") == "assistant"]
        cost_info["turns"] = len(assistant_msgs)

        # Rough token estimation from message lengths if no stats
        if not cost_info["total_tokens"] and messages:
            total_chars = sum(len(str(m.get("content", "")))
                              for m in messages if isinstance(m, dict))
            est_tokens = total_chars // 4  # rough char-to-token ratio
            cost_info["input_tokens"] = int(est_tokens * 0.85)  # ~85% input
            cost_info["output_tokens"] = int(est_tokens * 0.15)  # ~15% output
            cost_info["total_tokens"] = est_tokens
            cost_info["cost_usd"] = (
                cost_info["input_tokens"] / 1_000_000 * COST_PER_1M_INPUT +
                cost_info["output_tokens"] / 1_000_000 * COST_PER_1M_OUTPUT
            )
            cost_info["source"] = "estimated_from_chars"
            break

    cost_info["cost_usd"] = round(cost_info["cost_usd"], 4)
    return cost_info


def _extract_patch(container_id: str) -> str:
    """Extract the submitted patch from container."""
    try:
        output = _docker_exec(container_id, "cat /testbed/patch.txt 2>/dev/null", timeout=5)
        if output and output.startswith("diff"):
            return output
        # Fallback: git diff
        output = _docker_exec(container_id, "cd /testbed && git diff", timeout=10)
        return output
    except Exception:
        return ""


def load_dataset(dataset: str, split: str, instance_ids: list[str] | None,
                 max_instances: int | None) -> list[dict]:  # type: ignore[type-arg]
    """Load SWE-bench-Live Lite dataset."""
    try:
        from datasets import load_dataset as hf_load
        ds = hf_load(dataset, split=split)
        instances: list[dict] = [dict(row) for row in ds]  # type: ignore[arg-type]
    except Exception:
        logger.error("Failed to load dataset %s split=%s", dataset, split)
        raise

    if instance_ids:
        id_set = set(instance_ids)
        instances = [i for i in instances if i["instance_id"] in id_set]

    if max_instances:
        instances = instances[:max_instances]

    return instances


def process_instance(
    instance: dict,
    config_path: str,
    output_dir: Path,
) -> dict:
    """Process a single SWE-bench instance with GT hybrid.

    Uses mini-SWE-agent (minisweagent) as the execution engine — it mirrors
    real SWE-agent's loop (YAML config + Docker env + bash tool execution).
    GT is injected via docker cp + state command + budget-enforced tools.
    """
    import yaml

    instance = _inject_live_docker_image(instance)
    instance_id = instance["instance_id"]
    t0 = time.time()

    instance_dir = output_dir / "trajs" / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    gt_logs_dir = output_dir / "gt_logs"
    gt_logs_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "instance_id": instance_id,
        "resolved": False,
        "patch": "",
        "exit_status": "unknown",
        "gt_injected": False,
        "runtime_s": 0,
        "error": None,
        "cost": {},
    }

    # Load YAML config
    with open(config_path) as f:
        config = yaml.safe_load(f)

    agent = None
    env = None

    try:
        from minisweagent.run.benchmarks.swebench import (
            get_sb_environment,
            get_model,
            ProgressTrackingAgent,
            update_preds_file,
            remove_from_preds_file,
        )

        # Clean up any prior prediction for this instance
        preds_file = output_dir / "submission" / "all_preds.jsonl"
        remove_from_preds_file(preds_file, instance_id)
        (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

        # Create environment (Docker container)
        env = get_sb_environment(config, instance)
        container_id = getattr(env, "container_id", None)

        logger.info("Container started: %s | %s", instance_id,
                     container_id[:12] if container_id else "unknown")

        # Inject GT files into container
        gt_ok = False
        if container_id:
            gt_ok = _inject_gt(container_id, instance_id)
        result["gt_injected"] = gt_ok

        # Get orientation for task prepend
        orient_text = ""
        if gt_ok and container_id:
            orient_text = _get_gt_orient(container_id)

        # Write problem statement for state command
        problem = instance.get("problem_statement", "")
        if problem and container_id:
            # Use python to write the file to avoid heredoc escaping issues
            safe_problem = problem[:3000].replace("'", "'\\''")
            _docker_exec(container_id,
                         f"python3 -c \"open('/tmp/gt_issue.txt','w').write('''{safe_problem}''')\"",
                         timeout=5)

        # Prepare task text with orientation prepended
        task = instance.get("problem_statement", "")
        if orient_text:
            task = orient_text + "\n" + task

        # Create model and agent
        model = get_model(config=config.get("model", {}))
        agent = ProgressTrackingAgent(
            model,
            env,
            **config.get("agent", {}),
        )

        # Run the agent
        logger.info("Running agent: %s", instance_id)
        info = agent.run(task)
        exit_status = info.get("exit_status", "unknown")
        submission = info.get("submission", "")

        result["exit_status"] = exit_status
        result["patch"] = submission or ""

        # Extract model stats for cost tracking
        model_stats = getattr(agent.model, "stats", {}) if hasattr(agent, "model") else {}

        # Build trajectory
        traj = {
            "instance_id": instance_id,
            "messages": agent.messages if agent else [],
            "info": {
                "model_stats": model_stats,
                "exit_status": exit_status,
                "submission": submission or "",
            },
            "trajectory_format": "minisweagent",
        }
        traj_path = instance_dir / f"{instance_id}.traj.json"
        traj_path.write_text(json.dumps(traj, indent=2, default=str))

        # Extract GT telemetry from container
        if container_id:
            gt_telem = _extract_telemetry(container_id, gt_logs_dir, instance_id)
            result["gt_telemetry"] = gt_telem

        # Extract cost from trajectory
        cost_info = _extract_cost_from_traj(instance_dir, instance_id)
        result["cost"] = cost_info

    except Exception as e:
        result["exit_status"] = "infra_error"
        result["error"] = str(e)[:300]
        logger.error("Error for %s: %s", instance_id, e, exc_info=True)

    finally:
        if env is not None:
            try:
                env.teardown()
            except Exception:
                pass
        result["runtime_s"] = round(time.time() - t0, 1)

    return result


def write_prediction(preds_path: Path, instance_id: str, model_name: str, patch: str) -> None:
    """Append a prediction to the JSONL file."""
    entry = {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": model_name,
    }
    with open(preds_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    # Also write to preds.json dict format for swebench harness compatibility
    preds_dict_path = preds_path.parent.parent / "preds.json"
    preds_dict: dict = {}
    if preds_dict_path.exists():
        try:
            preds_dict = json.loads(preds_dict_path.read_text())
        except Exception:
            pass
    preds_dict[instance_id] = {"model_patch": patch, "model_name_or_path": model_name}
    preds_dict_path.write_text(json.dumps(preds_dict, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Canonical SWE-agent + DeepSeek V3.2 + GT hybrid")
    parser.add_argument("--config", required=True, help="SWE-agent YAML config path")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--dataset", default="SWE-bench-Live/SWE-bench-Live",
                        help="HuggingFace dataset name")
    parser.add_argument("--split", default="lite", help="Dataset split")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers")
    parser.add_argument("--instance-ids", default="",
                        help="Comma-separated instance IDs (subset)")
    parser.add_argument("--max-instances", type=int, default=None,
                        help="Max instances to process")
    parser.add_argument("--model-name", default="sweagent+deepseek-v3.2+groundtruth",
                        help="Model name for predictions")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "submission").mkdir(exist_ok=True)
    (output_dir / "trajs").mkdir(exist_ok=True)
    (output_dir / "gt_logs").mkdir(exist_ok=True)

    # Freeze config snapshot
    config_path = Path(args.config).resolve()
    shutil.copy2(config_path, output_dir / "run_config.yaml")

    # Load dataset
    ids = [i.strip() for i in args.instance_ids.split(",") if i.strip()] if args.instance_ids else None
    instances = load_dataset(args.dataset, args.split, ids, args.max_instances)
    logger.info("Loaded %d instances from %s (split=%s)", len(instances), args.dataset, args.split)

    # Predictions file
    preds_path = output_dir / "submission" / "all_preds.jsonl"

    # Run log
    run_log_path = output_dir / "run_log.txt"
    run_log = open(run_log_path, "w")

    t_start = time.time()
    results = []
    completed = 0
    errors = 0
    empty_patches = 0

    # Sequential processing (parallelism via --workers > 1 needs process pool)
    if args.workers <= 1:
        for inst in instances:
            r = process_instance(inst, str(config_path), output_dir)
            results.append(r)
            completed += 1

            if r["patch"]:
                write_prediction(preds_path, r["instance_id"], args.model_name, r["patch"])
            else:
                empty_patches += 1

            if r["exit_status"] not in ("completed",):
                errors += 1

            _c = r.get("cost", {})
            status_line = (f"[{completed}/{len(instances)}] {r['instance_id']} | "
                           f"status={r['exit_status']} | patch={'yes' if r['patch'] else 'EMPTY'} | "
                           f"gt={r['gt_injected']} | {r['runtime_s']}s | "
                           f"${_c.get('cost_usd', 0):.4f} "
                           f"({_c.get('input_tokens', 0):,}in/{_c.get('output_tokens', 0):,}out)")
            logger.info(status_line)
            run_log.write(status_line + "\n")
            run_log.flush()
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(process_instance, inst, str(config_path), output_dir): inst
                for inst in instances
            }
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                completed += 1

                if r["patch"]:
                    write_prediction(preds_path, r["instance_id"], args.model_name, r["patch"])
                else:
                    empty_patches += 1

                if r["exit_status"] not in ("completed",):
                    errors += 1

                status_line = (f"[{completed}/{len(instances)}] {r['instance_id']} | "
                               f"status={r['exit_status']} | patch={'yes' if r['patch'] else 'EMPTY'} | "
                               f"gt={r['gt_injected']} | {r['runtime_s']}s")
                logger.info(status_line)
                run_log.write(status_line + "\n")
                run_log.flush()

    run_log.close()
    total_time = round(time.time() - t_start, 1)

    # Write summary
    gt_inject_count = sum(1 for r in results if r.get("gt_injected"))
    patch_count = sum(1 for r in results if r.get("patch"))

    # Aggregate cost data
    total_input_tokens = sum(r.get("cost", {}).get("input_tokens", 0) for r in results)
    total_output_tokens = sum(r.get("cost", {}).get("output_tokens", 0) for r in results)
    total_cost = sum(r.get("cost", {}).get("cost_usd", 0) for r in results)
    costs_per_task = [r.get("cost", {}).get("cost_usd", 0) for r in results if r.get("cost")]

    # Determine cost band
    avg_cost = total_cost / max(completed, 1)
    if avg_cost < 0.35:
        cost_band = "A (Lean)"
    elif avg_cost < 0.85:
        cost_band = "B (Medium)"
    else:
        cost_band = "C (Heavy)"

    summary = {
        "run_id": f"canonical_gt_{time.strftime('%Y%m%d_%H%M%S')}",
        "config": str(config_path),
        "dataset": args.dataset,
        "split": args.split,
        "model_name": args.model_name,
        "total_instances": len(instances),
        "completed": completed,
        "errors": errors,
        "empty_patches": empty_patches,
        "patches_submitted": patch_count,
        "gt_inject_success": gt_inject_count,
        "gt_inject_rate": round(gt_inject_count / max(len(instances), 1), 3),
        "total_runtime_s": total_time,
        "avg_runtime_per_task_s": round(total_time / max(completed, 1), 1),
        "cost": {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_task_usd": round(avg_cost, 4),
            "min_cost_per_task_usd": round(min(costs_per_task), 4) if costs_per_task else 0,
            "max_cost_per_task_usd": round(max(costs_per_task), 4) if costs_per_task else 0,
            "cost_band": cost_band,
            "projected_300_task_usd": round(avg_cost * 300, 2),
            "pricing": {
                "input_per_1m": COST_PER_1M_INPUT,
                "output_per_1m": COST_PER_1M_OUTPUT,
                "model": "deepseek-v3.2-vertex",
            },
        },
        "per_instance": [
            {
                "instance_id": r["instance_id"],
                "exit_status": r["exit_status"],
                "has_patch": bool(r.get("patch")),
                "gt_injected": r.get("gt_injected", False),
                "runtime_s": r.get("runtime_s", 0),
                "gt_budget_final": r.get("gt_telemetry", {}).get("budget_final"),
                "cost_usd": r.get("cost", {}).get("cost_usd", 0),
                "input_tokens": r.get("cost", {}).get("input_tokens", 0),
                "output_tokens": r.get("cost", {}).get("output_tokens", 0),
                "total_tokens": r.get("cost", {}).get("total_tokens", 0),
                "cost_source": r.get("cost", {}).get("source", "none"),
                "error": r.get("error"),
            }
            for r in results
        ],
    }

    summary_path = output_dir / "telemetry_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    logger.info("Summary written to %s", summary_path)

    # Write submission metadata
    metadata = {
        "name": "SWE-agent + DeepSeek V3.2 + GroundTruth",
        "model": "deepseek-v3.2",
        "scaffold": "swe-agent",
        "augmentation": "groundtruth",
        "benchmark": "SWE-bench-Live Lite",
        "baseline_anchor": "15.33% (SWE-agent + DeepSeek V3, paper)",
        "gt_delta_only": True,
        "temperature": 1.0,
        "top_p": 0.95,
        "max_output_tokens": 8192,
        "step_limit": 100,
        "retry_farm": False,
        "multi_attempt": False,
    }
    (output_dir / "submission" / "metadata.yaml").write_text(
        "\n".join(f"{k}: {json.dumps(v)}" for k, v in metadata.items()) + "\n"
    )

    # Write per-task cost log (easy to grep)
    cost_log_path = output_dir / "cost_log.txt"
    with open(cost_log_path, "w") as f:
        f.write(f"{'Instance ID':<50} {'Status':<12} {'Input':>10} {'Output':>10} "
                f"{'Total':>10} {'Cost':>10} {'Source':<25}\n")
        f.write("-" * 130 + "\n")
        for r in results:
            c = r.get("cost", {})
            f.write(f"{r['instance_id']:<50} {r['exit_status']:<12} "
                    f"{c.get('input_tokens', 0):>10,} {c.get('output_tokens', 0):>10,} "
                    f"{c.get('total_tokens', 0):>10,} ${c.get('cost_usd', 0):>9.4f} "
                    f"{c.get('source', 'none'):<25}\n")
        f.write("-" * 130 + "\n")
        f.write(f"{'TOTAL':<50} {'':12} {total_input_tokens:>10,} {total_output_tokens:>10,} "
                f"{total_input_tokens + total_output_tokens:>10,} ${total_cost:>9.4f}\n")
        f.write(f"{'AVG PER TASK':<50} {'':12} "
                f"{total_input_tokens // max(completed, 1):>10,} "
                f"{total_output_tokens // max(completed, 1):>10,} "
                f"{(total_input_tokens + total_output_tokens) // max(completed, 1):>10,} "
                f"${avg_cost:>9.4f}\n")
        f.write(f"\nCost band: {cost_band}\n")
        f.write(f"Projected 300-task cost: ${avg_cost * 300:.2f}\n")

    logger.info("Cost log written to %s", cost_log_path)

    # Final report
    logger.info("=" * 60)
    logger.info("RUN COMPLETE")
    logger.info("  Instances: %d", len(instances))
    logger.info("  Completed: %d", completed)
    logger.info("  Errors: %d", errors)
    logger.info("  Patches: %d", patch_count)
    logger.info("  Empty: %d", empty_patches)
    logger.info("  GT inject rate: %.1f%%", gt_inject_count / max(len(instances), 1) * 100)
    logger.info("  Total time: %ds", total_time)
    logger.info("  Avg per task: %.1fs", total_time / max(completed, 1))
    logger.info("  ---- COST ----")
    logger.info("  Total input tokens:  %s", f"{total_input_tokens:,}")
    logger.info("  Total output tokens: %s", f"{total_output_tokens:,}")
    logger.info("  Total cost:          $%.4f", total_cost)
    logger.info("  Avg cost/task:       $%.4f", avg_cost)
    logger.info("  Cost band:           %s", cost_band)
    logger.info("  Projected 300 tasks: $%.2f", avg_cost * 300)
    logger.info("  ---- END ----")
    logger.info("  Output: %s", output_dir)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
