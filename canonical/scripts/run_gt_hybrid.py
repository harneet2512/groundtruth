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
import sys
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
    """Process a single SWE-bench instance with GT hybrid."""
    instance = _inject_live_docker_image(instance)
    instance_id = instance["instance_id"]
    t0 = time.time()

    instance_dir = output_dir / "trajs" / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    gt_logs_dir = output_dir / "gt_logs"
    gt_logs_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "instance_id": instance_id,
        "resolved": False,
        "patch": "",
        "exit_status": "unknown",
        "gt_injected": False,
        "runtime_s": 0,
        "error": None,
    }

    container_id = None
    try:
        # Start Docker container
        docker_image = instance.get("docker_image", "")
        container_id = sp.run(
            ["docker", "run", "-d", "--rm", "--entrypoint", "",
             "-w", "/testbed", docker_image, "sleep", "7200"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout.strip()

        logger.info("Container started: %s | %s", instance_id, container_id[:12])

        # Inject GT
        gt_ok = _inject_gt(container_id, instance_id)
        result["gt_injected"] = gt_ok

        # Get orientation for task prepend
        orient_text = ""
        if gt_ok:
            orient_text = _get_gt_orient(container_id)

        # Write problem statement for state command
        problem = instance.get("problem_statement", "")
        if problem:
            _docker_exec(container_id,
                         f"cat > /tmp/gt_issue.txt << 'GTEOF'\n{problem[:3000]}\nGTEOF")

        # Build SWE-agent command
        # Write instance to temp file for SWE-agent
        instance_file = instance_dir / "instance.json"
        enriched_instance = dict(instance)
        if orient_text:
            enriched_instance["problem_statement"] = orient_text + problem

        instance_file.write_text(json.dumps(enriched_instance, default=str))

        # Run SWE-agent via CLI on this container
        # SWE-agent's run command with the GT config
        sweagent_cmd = [
            sys.executable, "-m", "sweagent", "run",
            "--config", config_path,
            "--instance", str(instance_file),
            "--output-dir", str(instance_dir),
        ]

        logger.info("Running SWE-agent: %s", instance_id)
        proc = sp.run(
            sweagent_cmd,
            capture_output=True, text=True,
            timeout=1800,  # 30 min max per instance
            env={**os.environ, "DOCKER_CONTAINER_ID": container_id},
        )

        if proc.returncode == 0:
            result["exit_status"] = "completed"
        else:
            result["exit_status"] = f"error_{proc.returncode}"
            result["error"] = proc.stderr[-500:] if proc.stderr else None
            logger.warning("SWE-agent error for %s: %s", instance_id,
                           proc.stderr[-200:] if proc.stderr else "no stderr")

        # Extract patch
        patch = _extract_patch(container_id)
        result["patch"] = patch

        # Extract GT telemetry
        gt_telem = _extract_telemetry(container_id, gt_logs_dir, instance_id)
        result["gt_telemetry"] = gt_telem

        # Read trajectory if SWE-agent wrote one
        traj_files = list(instance_dir.glob("*.traj*"))
        if traj_files:
            result["trajectory_path"] = str(traj_files[0])

    except sp.TimeoutExpired:
        result["exit_status"] = "timeout"
        result["error"] = "Instance timed out after 1800s"
        logger.error("Timeout: %s", instance_id)

    except Exception as e:
        result["exit_status"] = "infra_error"
        result["error"] = str(e)[:300]
        logger.error("Infra error for %s: %s", instance_id, e)

    finally:
        # Cleanup container
        if container_id:
            try:
                sp.run(["docker", "rm", "-f", container_id],
                       capture_output=True, timeout=10)
            except Exception:
                pass

        result["runtime_s"] = round(time.time() - t0, 1)

    return result


def write_prediction(preds_path: Path, instance_id: str, model_name: str, patch: str) -> None:
    """Append a prediction to the JSONL file (thread-safe via append mode)."""
    entry = {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": model_name,
    }
    with open(preds_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


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

            status_line = (f"[{completed}/{len(instances)}] {r['instance_id']} | "
                           f"status={r['exit_status']} | patch={'yes' if r['patch'] else 'EMPTY'} | "
                           f"gt={r['gt_injected']} | {r['runtime_s']}s")
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
        "per_instance": [
            {
                "instance_id": r["instance_id"],
                "exit_status": r["exit_status"],
                "has_patch": bool(r.get("patch")),
                "gt_injected": r.get("gt_injected", False),
                "runtime_s": r.get("runtime_s", 0),
                "gt_budget_final": r.get("gt_telemetry", {}).get("budget_final"),
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
    logger.info("  Output: %s", output_dir)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
