#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with GroundTruth context injection.

Strategy: For each task, we run the GT context script INSIDE the Docker
container (where /testbed has the actual repo at the right commit), capture
the output, and prepend it to the problem_statement before mini-swe-agent
processes the task.

This is done by monkey-patching the process_instance function to add a
pre-processing step that generates GT context inside the container.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# mini-swe-agent imports
from minisweagent.run.benchmarks.swebench import (
    app,
    main as swebench_main,
    process_instance as original_process_instance,
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


def gt_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Wrap process_instance to inject GT context."""
    import traceback

    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale predictions
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
    gt_context = ""

    try:
        env = get_sb_environment(config, instance)

        # --- GT INJECTION ---
        # Copy the context script into the container and run it
        progress_manager.update_instance_status(instance_id, "GT: indexing")
        try:
            # Read the script content
            script_content = GT_SCRIPT_PATH.read_text()

            # Write script to container via echo (avoid docker cp complexity)
            # Escape for bash
            escaped = script_content.replace("\\", "\\\\").replace("'", "'\\''")
            write_cmd = f"cat > /tmp/gt_context.py << 'GTEOF'\n{script_content}\nGTEOF"
            env.execute(write_cmd)

            # Run the context generator with the problem statement
            # Escape problem statement for shell
            problem_escaped = original_task.replace("'", "'\\''")
            gen_cmd = f"cd /testbed && python3 /tmp/gt_context.py '{problem_escaped}' /testbed 2>/dev/null"
            gen_result = env.execute(gen_cmd)

            if gen_result.get("returncode", 1) == 0:
                gt_context = gen_result.get("output", "").strip()
                if gt_context:
                    logger.info(f"GT context generated for {instance_id}: {len(gt_context)} chars")
                else:
                    logger.info(f"GT context empty for {instance_id}")
            else:
                logger.warning(f"GT context generation failed for {instance_id}: {gen_result.get('output', '')[:200]}")

        except Exception as e:
            logger.warning(f"GT injection failed for {instance_id}: {e}")
            gt_context = ""

        # Modify the task with GT context prepended
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
        logger.error(f"Error processing instance {instance_id}: {e}", exc_info=True)
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
                        "gt_context": gt_context,
                        "gt_context_chars": len(gt_context),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info(f"Saved trajectory to '{traj_path}'")
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch the process_instance function
swebench_module.process_instance = gt_process_instance

if __name__ == "__main__":
    # Run the standard swebench CLI with our patched process_instance
    app()
