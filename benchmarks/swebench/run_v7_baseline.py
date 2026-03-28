#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench WITHOUT GroundTruth — clean baseline.

This is the control condition for v7 A/B comparison.
Identical agent config, same model, same prompt structure — but:
  - No gt_hook.py injection
  - No understand/verify instructions in the prompt
  - No GT tool references whatsoever

The prompt template is a clean version of the v7 template with all GT
instructions stripped, ensuring the agent operates with standard tooling only.

Usage:
    python run_v7_baseline.py swebench --model openai/qwen3-coder \
        --subset lite --split test --slice 0:10 -w 2 -o ~/results/v7_baseline
"""
from __future__ import annotations

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



def baseline_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Process instance with NO GT injection — clean baseline."""
    instance_id = instance["instance_id"]
    # Map Pro dockerhub_tag to docker_image for mini-swe-agent compatibility
    if "docker_image" not in instance and "dockerhub_tag" in instance:
        instance["docker_image"] = f"jefzda/sweap-images:{instance['dockerhub_tag']}"
    instance_dir = output_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting environment")

    agent = None
    exit_status = None
    result = None
    extra_info: dict = {}

    try:
        env = get_sb_environment(config, instance)

        # --- NO GT SETUP — this is the baseline ---

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
        extra_info["traceback"] = traceback.format_exc()
        extra_info["exception_str"] = str(e)
    finally:
        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "v7_baseline",
                        "gt_delivery": "none",
                        "gt_tool_available": False,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)

        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch mini-swe-agent's process_instance
swebench_module.process_instance = baseline_process_instance

if __name__ == "__main__":
    app()
