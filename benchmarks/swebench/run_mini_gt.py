#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with GroundTruth context injection.

Strategy: For each task, we copy our self-contained GT context script into
the Docker container (where /testbed has the repo at the right commit),
run it to generate a context block, and prepend it to the problem_statement.

We monkey-patch mini-swe-agent's process_instance to add this step.

v2: Now captures full observability metrics (class structure, keywords,
    coupling info) from the smart context generator.
"""
from __future__ import annotations

import base64
import json
import traceback
from pathlib import Path

# mini-swe-agent imports
from minisweagent.run.benchmarks.swebench import (
    app,
    get_sb_environment,
    get_model,
    ProgressTrackingAgent,
    update_preds_file,
    remove_from_preds_file,
    logger,
)
from minisweagent.run.benchmarks import swebench as swebench_module

# Path to our self-contained GT context script
GT_SCRIPT_PATH = Path(__file__).parent / "mini_gt_context.py"
# Pre-encode script as base64 to avoid shell escaping issues
_GT_SCRIPT_B64 = base64.b64encode(GT_SCRIPT_PATH.read_bytes()).decode("ascii")


def _exec(env, cmd: str, timeout: int = 60) -> dict:
    """Execute a command in the environment, handling the dict action format."""
    return env.execute({"command": cmd}, timeout=timeout)


def _generate_gt_context(env, problem_statement: str, instance_id: str) -> dict:
    """Copy GT script into container, run it, return full result dict.

    Returns dict with keys: context, metrics, keywords, top_classes, top_functions.
    On failure returns dict with empty context and error info.
    """
    empty_result = {
        "context": "",
        "metrics": {},
        "keywords": [],
        "top_classes": [],
        "top_functions": [],
    }

    try:
        # Write script via base64 decode (avoids all quoting issues)
        _exec(env, f"echo '{_GT_SCRIPT_B64}' | base64 -d > /tmp/gt_context.py")

        # Write problem statement to a file to avoid shell escaping
        # Use base64 for the problem statement too
        problem_b64 = base64.b64encode(problem_statement.encode()).decode("ascii")
        _exec(env, f"echo '{problem_b64}' | base64 -d > /tmp/gt_problem.txt")

        # Run the context generator — now outputs JSON
        result = _exec(
            env,
            "cd /testbed && python3 /tmp/gt_context.py /testbed /tmp/gt_problem.txt 2>/dev/null",
            timeout=30,
        )

        if result.get("returncode", 1) == 0:
            output = result.get("output", "").strip()
            if output:
                try:
                    gt_result = json.loads(output)
                    context = gt_result.get("context", "")
                    metrics = gt_result.get("metrics", {})
                    logger.info(
                        "GT v2 context for %s: %d chars, %d classes matched, %d in context, %.1fs",
                        instance_id,
                        len(context),
                        metrics.get("classes_matched", 0),
                        metrics.get("classes_in_context", 0),
                        metrics.get("total_time_seconds", 0),
                    )
                    return gt_result
                except json.JSONDecodeError:
                    # Fallback: treat raw output as context string (v1 compat)
                    logger.warning(
                        "GT context for %s: JSON parse failed, using raw output (%d chars)",
                        instance_id, len(output),
                    )
                    empty_result["context"] = output
                    return empty_result
            logger.info("GT context empty for %s", instance_id)
        else:
            logger.warning(
                "GT context failed for %s: rc=%s", instance_id, result.get("returncode"),
            )
    except Exception as e:
        logger.warning("GT injection error for %s: %s", instance_id, e)
        empty_result["error"] = str(e)
    return empty_result


def gt_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Wrap process_instance to inject GT context."""
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

    model = get_model(config=config.get("model", {}))
    original_task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting environment")

    agent = None
    exit_status = None
    result = None
    extra_info = {}
    gt_result = {"context": "", "metrics": {}, "keywords": [], "top_classes": [], "top_functions": []}

    try:
        env = get_sb_environment(config, instance)

        # --- GT INJECTION ---
        progress_manager.update_instance_status(instance_id, "GT: indexing")
        gt_result = _generate_gt_context(env, original_task, instance_id)
        gt_context = gt_result.get("context", "")

        # Prepend GT context to the task
        if gt_context:
            task = f"{gt_context}\n\n---\n\n{original_task}"
        else:
            task = original_task

        # --- RUN AGENT ---
        progress_manager.update_instance_status(instance_id, "Step   1")
        agent = ProgressTrackingAgent(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **config.get("agent", {}),
        )
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")

    except Exception as e:
        logger.error("Error processing %s: %s", instance_id, e, exc_info=True)
        exit_status, result = type(e).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}
    finally:
        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        # v2: full observability
                        "gt_context": gt_result.get("context", ""),
                        "gt_context_chars": len(gt_result.get("context", "")),
                        "gt_metrics": gt_result.get("metrics", {}),
                        "gt_keywords": gt_result.get("keywords", []),
                        "gt_top_classes": gt_result.get("top_classes", []),
                        "gt_top_functions": gt_result.get("top_functions", []),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch
swebench_module.process_instance = gt_process_instance

if __name__ == "__main__":
    app()
