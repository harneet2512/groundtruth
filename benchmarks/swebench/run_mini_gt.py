#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with GroundTruth on-demand tools (v4).

Strategy: For each task, we copy gt_tool.py into the Docker container
(where /testbed has the repo at the right commit) and let the agent call
the tool on demand during its work. The tool builds its own index on
first invocation. The problem
statement is never modified — the agent decides when to query GT.

We monkey-patch mini-swe-agent's process_instance to add the setup step.

v4: On-demand tool delivery (replaces pre-computed file delivery).
    The agent calls `python3 /tmp/gt_tool.py <command> <args>` when it
    needs structural answers about the codebase.
"""
from __future__ import annotations

import base64
import json
import re
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

# Path to our self-contained GT tool script
GT_SCRIPT_PATH = Path(__file__).parent / "gt_tool.py"
# Pre-encode script as base64 to avoid shell escaping issues
_GT_SCRIPT_B64 = base64.b64encode(GT_SCRIPT_PATH.read_bytes()).decode("ascii")


def _exec(env, cmd: str, timeout: int = 60) -> dict:
    """Execute a command in the environment, handling the dict action format."""
    return env.execute({"command": cmd}, timeout=timeout)


def _setup_gt_tool(env, instance_id: str) -> dict:
    """Copy gt_tool.py into container for on-demand use.

    The tool builds its own index on first invocation by the agent.
    Returns dict with key: tool_available.
    """
    setup_result = {"tool_available": False}

    try:
        # Write script via base64 decode (avoids all quoting issues)
        _exec(env, f"echo '{_GT_SCRIPT_B64}' | base64 -d > /tmp/gt_tool.py")
        _exec(env, "chmod +x /tmp/gt_tool.py")
        setup_result["tool_available"] = True
        logger.info("GT v4 tool copied for %s", instance_id)
    except Exception as e:
        logger.warning("GT tool setup error for %s: %s", instance_id, e)
        setup_result["error"] = str(e)

    return setup_result


def _check_gt_tool_usage(traj_path: Path) -> dict:
    """Scan saved trajectory for gt_tool.py invocations."""
    usage: dict = {
        "any_call": False,
        "total_calls": 0,
        "first_call_turn": None,
        "commands_used": [],
        "symbols_queried": [],
        "total_turns": 0,
    }

    gt_pattern = re.compile(r"gt_tool\.py\s+(references|outline|coupled|impact|help)(?:\s+(\S+))?")

    try:
        with open(traj_path) as f:
            traj = json.load(f)
        messages = (traj.get("history") or traj.get("messages")
                    or traj.get("trajectory") or [])
        usage["total_turns"] = len(messages)

        commands_seen = set()
        symbols_seen = set()

        for i, msg in enumerate(messages):
            content = str(msg.get("content", "") if isinstance(msg, dict) else msg)
            for match in gt_pattern.finditer(content):
                if not usage["any_call"]:
                    usage["any_call"] = True
                    usage["first_call_turn"] = i
                usage["total_calls"] += 1
                commands_seen.add(match.group(1))
                if match.group(2):
                    symbols_seen.add(match.group(2))

        usage["commands_used"] = sorted(commands_seen)
        usage["symbols_queried"] = sorted(symbols_seen)
    except Exception:
        pass

    return usage


def gt_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Wrap process_instance to set up GT tool for on-demand use."""
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
    gt_setup = {"tool_available": False}

    try:
        env = get_sb_environment(config, instance)

        # --- GT TOOL SETUP ---
        progress_manager.update_instance_status(instance_id, "GT: setting up tool")
        gt_setup = _setup_gt_tool(env, instance_id)

        task = original_task  # NEVER modify the problem statement

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
                        "gt_version": "v4_ondemand_tools",
                        "gt_delivery": "tool",
                        "gt_tool_available": gt_setup.get("tool_available", False),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)

            # Post-save: scan trajectory for GT tool usage evidence
            gt_tool_usage = _check_gt_tool_usage(traj_path)
            try:
                with open(traj_path) as f:
                    traj_data = json.load(f)
                traj_data.setdefault("info", {})["gt_tool_usage"] = gt_tool_usage
                with open(traj_path, "w") as f:
                    json.dump(traj_data, f)
            except Exception:
                pass
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch
swebench_module.process_instance = gt_process_instance

if __name__ == "__main__":
    app()
