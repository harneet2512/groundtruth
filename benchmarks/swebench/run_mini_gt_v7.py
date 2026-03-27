#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with GroundTruth v7 (constraint map).

v7 = Cross-file intelligence: callers, test files, norms.
gt_hook.py is injected via chunked base64 into /tmp/gt_hook.py.
Agent calls `python3 /tmp/gt_hook.py understand <filepath>` during exploration.
Agent optionally calls `python3 /tmp/gt_hook.py verify` after editing.

The problem statement is never modified -- the agent decides when to query GT.
Hook logs are extracted from containers after runs for analysis.

Usage:
    python run_mini_gt_v7.py swebench --model openai/qwen3-coder \
        --subset lite --split test --slice 0:10 -w 2 -o ~/results/v7
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

# ---------------------------------------------------------------------------
# GT hook injection
# ---------------------------------------------------------------------------

GT_HOOK_PATH = Path(__file__).parent / "gt_hook.py"

# Pre-encode at import time. The file is ~115KB -> ~153KB base64.
# We split into chunks to avoid shell argument length limits.
_GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii")
_CHUNK_SIZE = 50_000  # ~50KB chunks -> ~3 chunks total
_GT_HOOK_CHUNKS = [
    _GT_HOOK_B64[i : i + _CHUNK_SIZE]
    for i in range(0, len(_GT_HOOK_B64), _CHUNK_SIZE)
]

logger.info(
    "GT v7 hook: %d bytes, %d base64 chars, %d chunks",
    GT_HOOK_PATH.stat().st_size,
    len(_GT_HOOK_B64),
    len(_GT_HOOK_CHUNKS),
)


def _exec(env, cmd: str, timeout: int = 60) -> dict:
    """Execute a command in the environment, handling the dict action format."""
    return env.execute({"command": cmd}, timeout=timeout)


def _setup_gt_hook(env, instance_id: str) -> dict:
    """Inject gt_hook.py into the container via chunked base64.

    The file is too large for a single echo command (~153KB base64),
    so we split into ~50KB chunks, write each with echo append,
    then base64 decode at the end.

    Returns dict with setup status.
    """
    setup_result = {
        "tool_available": False,
        "chunks_written": 0,
        "prewarm": False,
    }

    try:
        # Write chunks to a temp base64 file
        for i, chunk in enumerate(_GT_HOOK_CHUNKS):
            op = ">" if i == 0 else ">>"
            _exec(env, f"echo -n '{chunk}' {op} /tmp/gt_hook.b64", timeout=15)
            setup_result["chunks_written"] = i + 1

        # Decode base64 to the final script location
        _exec(
            env,
            "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64",
            timeout=15,
        )
        _exec(env, "chmod +x /tmp/gt_hook.py", timeout=5)
        setup_result["tool_available"] = True

        # Pre-warm: run understand --help to verify the script loads without error
        try:
            result = _exec(
                env,
                "python3 /tmp/gt_hook.py understand --help 2>&1 | head -5",
                timeout=15,
            )
            setup_result["prewarm"] = True
            logger.info("GT v7 hook ready for %s", instance_id)
        except Exception as e:
            setup_result["prewarm"] = False
            logger.warning(
                "GT v7 prewarm failed for %s (hook still available): %s",
                instance_id, e,
            )

    except Exception as e:
        logger.warning("GT v7 hook setup error for %s: %s", instance_id, e)
        setup_result["error"] = str(e)

    return setup_result


def _extract_hook_log(env, instance_id: str, output_dir: Path) -> Path | None:
    """Extract /tmp/gt_hook_log.jsonl from the container after the run."""
    gt_log_dir = output_dir / "gt_logs"
    gt_log_dir.mkdir(parents=True, exist_ok=True)
    local_path = gt_log_dir / f"{instance_id}.jsonl"

    try:
        result = _exec(env, "cat /tmp/gt_hook_log.jsonl 2>/dev/null", timeout=10)
        log_content = result.get("output", "")
        if log_content.strip():
            local_path.write_text(log_content)
            logger.info("Extracted GT hook log for %s (%d bytes)", instance_id, len(log_content))
            return local_path
    except Exception as e:
        logger.debug("No GT hook log for %s: %s", instance_id, e)

    return None


def _check_gt_hook_usage(traj_path: Path) -> dict:
    """Scan saved trajectory for gt_hook.py invocations (understand/verify)."""
    usage: dict = {
        "any_call": False,
        "total_calls": 0,
        "understand_calls": 0,
        "verify_calls": 0,
        "first_call_turn": None,
        "files_understood": [],
        "total_turns": 0,
    }

    understand_pattern = re.compile(
        r"gt_hook\.py\s+understand\s+(\S+)"
    )
    verify_pattern = re.compile(
        r"gt_hook\.py\s+verify"
    )

    try:
        with open(traj_path) as f:
            traj = json.load(f)
        messages = (
            traj.get("history")
            or traj.get("messages")
            or traj.get("trajectory")
            or []
        )
        usage["total_turns"] = len(messages)

        files_seen: set[str] = set()
        call_turns: list[int] = []

        for i, msg in enumerate(messages):
            content = str(msg.get("content", "") if isinstance(msg, dict) else msg)

            for match in understand_pattern.finditer(content):
                # Skip template/example lines
                ctx_start = max(0, match.start() - 30)
                ctx_end = min(len(content), match.end() + 30)
                ctx = content[ctx_start:ctx_end]
                if "<filepath>" in ctx or "<" in ctx.split("understand", 1)[-1][:20]:
                    continue
                if not usage["any_call"]:
                    usage["any_call"] = True
                    usage["first_call_turn"] = i
                usage["total_calls"] += 1
                usage["understand_calls"] += 1
                call_turns.append(i)
                filepath = match.group(1)
                # Strip flags like --root, --quiet
                if not filepath.startswith("-"):
                    files_seen.add(filepath)

            for match in verify_pattern.finditer(content):
                ctx_start = max(0, match.start() - 30)
                ctx_end = min(len(content), match.end() + 30)
                ctx = content[ctx_start:ctx_end]
                if "<" in ctx.split("verify", 1)[-1][:15]:
                    continue
                if not usage["any_call"]:
                    usage["any_call"] = True
                    usage["first_call_turn"] = i
                usage["total_calls"] += 1
                usage["verify_calls"] += 1
                call_turns.append(i)

        usage["files_understood"] = sorted(files_seen)
        usage["call_turns"] = call_turns
        if call_turns:
            usage["last_call_turn"] = call_turns[-1]
            usage["call_density"] = len(call_turns) / max(len(messages), 1)
    except Exception:
        pass

    return usage


# ---------------------------------------------------------------------------
# Main process_instance override
# ---------------------------------------------------------------------------

def gt_v7_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Wrap process_instance to inject GT v7 hook for on-demand use."""
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
    env = None
    exit_status = None
    result = None
    extra_info: dict = {}
    gt_setup: dict = {"tool_available": False}

    try:
        env = get_sb_environment(config, instance)

        # --- GT HOOK SETUP (chunked base64 injection) ---
        progress_manager.update_instance_status(instance_id, "GT v7: injecting hook")
        gt_setup = _setup_gt_hook(env, instance_id)
        extra_info["gt_setup"] = gt_setup

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
        extra_info["traceback"] = traceback.format_exc()
        extra_info["exception_str"] = str(e)
    finally:
        # --- EXTRACT HOOK LOGS ---
        try:
            if env is not None:
                log_path = _extract_hook_log(env, instance_id, output_dir)
                if log_path:
                    extra_info["hook_log_path"] = str(log_path)
        except Exception:
            pass

        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "v7_constraint_map",
                        "gt_delivery": "hook",
                        "gt_tool_available": gt_setup.get("tool_available", False),
                        "gt_prewarm": gt_setup.get("prewarm", False),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)

            # Post-save: scan trajectory for GT hook usage evidence
            gt_hook_usage = _check_gt_hook_usage(traj_path)
            try:
                with open(traj_path) as f:
                    traj_data = json.load(f)
                traj_data.setdefault("info", {})["gt_hook_usage"] = gt_hook_usage
                with open(traj_path, "w") as f:
                    json.dump(traj_data, f)
                logger.info(
                    "GT v7 usage for %s: %d calls (%d understand, %d verify)",
                    instance_id,
                    gt_hook_usage["total_calls"],
                    gt_hook_usage["understand_calls"],
                    gt_hook_usage["verify_calls"],
                )
            except Exception:
                pass

        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch mini-swe-agent's process_instance
swebench_module.process_instance = gt_v7_process_instance

if __name__ == "__main__":
    app()
