#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with GroundTruth post-processing (Phase 2B).

Architecture: The agent works ALONE with zero GT tools during work. Full 250-turn
budget for coding. After the agent submits its patch, GT post-processes it using
a runtime introspection KB for hallucination-free corrections.

Three components:
1. Runtime KB: imports actual classes, uses dir()/inspect to build ground-truth
   class member lists (catches metaclass, mixin, descriptor methods AST misses)
2. Bounded test feedback: system prompt tells agent to run one targeted test
   before submitting (agent behavior, not a GT tool)
3. Post-processing autocorrect: green-lane corrections using runtime-accurate KB

v4: On-demand tool delivery
v6: Post-processing auto-correction
Phase 2B: Post-processing ONLY. Zero GT tools during agent work.
"""
from __future__ import annotations

import base64
import json
import re
import traceback
from pathlib import Path

# mini-swe-agent imports (optional — allows --help without the dependency)
try:
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
    _HAS_MINISWEAGENT = True
except ImportError:
    _HAS_MINISWEAGENT = False

# Path to the autocorrect engine
GT_AUTOCORRECT_PATH = Path(__file__).parent / "gt_autocorrect.py"
_GT_AUTOCORRECT_B64 = base64.b64encode(GT_AUTOCORRECT_PATH.read_bytes()).decode("ascii")

# Path to the runtime introspection KB builder
GT_RUNTIME_KB_PATH = Path(__file__).parent / "gt_runtime_kb.py"
_GT_RUNTIME_KB_B64 = base64.b64encode(GT_RUNTIME_KB_PATH.read_bytes()).decode("ascii")


def _exec(env, cmd: str, timeout: int = 60) -> dict:
    """Execute a command in the environment, handling the dict action format."""
    return env.execute({"command": cmd}, timeout=timeout)


def _setup_postprocessing(env, instance_id: str) -> dict:
    """Set up post-processing scripts in the container (NO agent-visible tools).

    Copies autocorrect and runtime KB scripts. Does NOT copy gt_tool.py.
    Does NOT pre-warm any index. The agent has no GT tools available.
    """
    setup_result = {
        "postprocessing_available": False,
        "runtime_kb_built": False,
        "runtime_kb_stats": {},
    }

    try:
        # Copy autocorrect engine (post-processing only)
        _exec(env, f"echo '{_GT_AUTOCORRECT_B64}' | base64 -d > /tmp/gt_autocorrect.py")

        # Copy runtime KB builder
        _exec(env, f"echo '{_GT_RUNTIME_KB_B64}' | base64 -d > /tmp/gt_runtime_kb.py")

        setup_result["postprocessing_available"] = True

        # Build runtime KB during setup (before agent starts)
        # This imports actual classes and introspects them — takes 5-20s
        try:
            kb_result = _exec(
                env,
                "cd /testbed && python3 /tmp/gt_runtime_kb.py 2>/dev/null",
                timeout=30,
            )
            kb_output = kb_result.get("output", "")
            try:
                kb_data = json.loads(kb_output)
                setup_result["runtime_kb_built"] = True
                setup_result["runtime_kb_stats"] = {
                    "total_classes": kb_data.get("total_classes", 0),
                    "total_methods": kb_data.get("total_methods", 0),
                    "import_successes": kb_data.get("import_successes", 0),
                    "import_failures": len(kb_data.get("import_failures", [])),
                    "build_time": kb_data.get("build_time", 0),
                }
                # Save runtime KB to disk for autocorrect to read
                _exec(
                    env,
                    f"echo '{kb_output}' > /tmp/gt_runtime_kb.json 2>/dev/null || true",
                    timeout=5,
                )
                # Use base64 for reliable transfer of JSON with special chars
                import base64 as b64mod
                kb_b64 = b64mod.b64encode(kb_output.encode()).decode("ascii")
                _exec(
                    env,
                    f"echo '{kb_b64}' | base64 -d > /tmp/gt_runtime_kb.json",
                    timeout=5,
                )
                logger.info(
                    "Runtime KB built for %s: %d classes, %d methods, %d import failures",
                    instance_id,
                    kb_data.get("total_classes", 0),
                    kb_data.get("total_methods", 0),
                    len(kb_data.get("import_failures", [])),
                )
            except (json.JSONDecodeError, ValueError):
                logger.warning("Runtime KB output not valid JSON for %s", instance_id)
        except Exception as e:
            logger.warning("Runtime KB build failed for %s: %s", instance_id, e)

    except Exception as e:
        logger.warning("Post-processing setup error for %s: %s", instance_id, e)
        setup_result["error"] = str(e)

    return setup_result


def _check_gt_tool_usage(traj_path: Path) -> dict:
    """Scan saved trajectory for gt_tool.py invocations.

    In Phase 2B, this MUST return zero calls. If it doesn't, the runner
    is leaking GT tools into the agent's environment.
    """
    usage: dict = {
        "any_call": False,
        "total_calls": 0,
        "first_call_turn": None,
        "commands_used": [],
        "symbols_queried": [],
        "total_turns": 0,
    }

    gt_pattern = re.compile(
        r"gt_tool\.py\s+(references|outline|impact|diagnose|check|help|search|scope|obligations|context|related|summary|diff)"
        r"(?:\s+(\S+))?"
    )

    try:
        with open(traj_path) as f:
            traj = json.load(f)
        messages = (traj.get("history") or traj.get("messages")
                    or traj.get("trajectory") or [])
        usage["total_turns"] = len(messages)

        commands_seen = set()
        symbols_seen = set()
        command_counts: dict[str, int] = {}
        call_turns: list[int] = []

        for i, msg in enumerate(messages):
            parts = []
            if isinstance(msg, dict):
                parts.append(str(msg.get("content", "") or ""))
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    parts.append(str(fn.get("arguments", "") or ""))
            else:
                parts.append(str(msg))
            content = "\n".join(parts)
            for match in gt_pattern.finditer(content):
                ctx_start = max(0, match.start() - 20)
                ctx_end = min(len(content), match.end() + 20)
                if '<' in content[ctx_start:ctx_end]:
                    continue
                if not usage["any_call"]:
                    usage["any_call"] = True
                    usage["first_call_turn"] = i
                usage["total_calls"] += 1
                cmd = match.group(1)
                commands_seen.add(cmd)
                command_counts[cmd] = command_counts.get(cmd, 0) + 1
                call_turns.append(i)
                if match.group(2):
                    symbols_seen.add(match.group(2))

        usage["commands_used"] = sorted(commands_seen)
        usage["symbols_queried"] = sorted(symbols_seen)
        usage["command_counts"] = command_counts
        usage["call_turns"] = call_turns
        if call_turns:
            usage["last_call_turn"] = call_turns[-1]
            usage["call_density"] = len(call_turns) / max(len(messages), 1)
    except Exception:
        pass

    return usage


def _check_test_execution(traj_path: Path) -> dict:
    """Scan trajectory for pytest/test execution by the agent."""
    test_info: dict = {
        "test_executed": False,
        "test_commands": [],
        "test_turn": None,
    }

    pytest_pattern = re.compile(
        r"(?:python3?\s+-m\s+pytest|pytest)\s+(\S+)"
    )

    try:
        with open(traj_path) as f:
            traj = json.load(f)
        messages = (traj.get("history") or traj.get("messages")
                    or traj.get("trajectory") or [])

        for i, msg in enumerate(messages):
            parts = []
            if isinstance(msg, dict):
                parts.append(str(msg.get("content", "") or ""))
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    parts.append(str(fn.get("arguments", "") or ""))
            else:
                parts.append(str(msg))
            content = "\n".join(parts)
            for match in pytest_pattern.finditer(content):
                # Skip template lines
                ctx_start = max(0, match.start() - 20)
                ctx_end = min(len(content), match.end() + 20)
                if '<' in content[ctx_start:ctx_end]:
                    continue
                test_info["test_executed"] = True
                if test_info["test_turn"] is None:
                    test_info["test_turn"] = i
                test_info["test_commands"].append(match.group(0)[:100])
    except Exception:
        pass

    return test_info


def gt_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Phase 2B: Agent works alone, GT post-processes after submission."""
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
    gt_setup = {"postprocessing_available": False}

    try:
        env = get_sb_environment(config, instance)

        # --- POST-PROCESSING SETUP (no agent-visible tools) ---
        progress_manager.update_instance_status(instance_id, "GT: setting up post-processing")
        gt_setup = _setup_postprocessing(env, instance_id)

        task = original_task  # NEVER modify the problem statement

        # --- RUN AGENT (no GT tools available) ---
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

        # --- GT POST-PROCESSING AUTOCORRECT ---
        original_patch = result
        if result and gt_setup.get("postprocessing_available"):
            try:
                ac_result = _exec(
                    env,
                    "cd /testbed && python3 /tmp/gt_autocorrect.py 2>/dev/null",
                    timeout=30,
                )
                ac_output = ac_result.get("output", "")
                ac_report: dict = {}
                try:
                    ac_report = json.loads(ac_output)
                except (json.JSONDecodeError, ValueError):
                    pass

                if ac_report.get("corrections"):
                    # Re-extract diff from the now-corrected files
                    patched_files = re.findall(
                        r'^\+\+\+ b/(.+)$', result, re.MULTILINE,
                    )
                    if patched_files:
                        file_args = " ".join(f"'{f}'" for f in patched_files)
                        corrected_result = _exec(
                            env,
                            f"cd /testbed && git diff -- {file_args}",
                            timeout=15,
                        )
                    else:
                        corrected_result = _exec(
                            env, "cd /testbed && git diff", timeout=15,
                        )
                    corrected_patch = corrected_result.get("output", "")
                    if corrected_patch:
                        result = corrected_patch

                extra_info["autocorrect_report"] = ac_report
                extra_info["original_patch"] = original_patch
            except Exception as e:
                logger.warning(
                    "Autocorrect failed for %s: %s", instance_id, e,
                )
                extra_info["autocorrect_error"] = str(e)

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
                        "gt_version": "phase2b",
                        "gt_delivery": "postprocessing_only",
                        "gt_postprocessing_available": gt_setup.get("postprocessing_available", False),
                        "gt_runtime_kb_built": gt_setup.get("runtime_kb_built", False),
                        "gt_runtime_kb_stats": gt_setup.get("runtime_kb_stats", {}),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)

            # Post-save: scan trajectory for GT tool usage (MUST be zero)
            gt_tool_usage = _check_gt_tool_usage(traj_path)
            test_execution = _check_test_execution(traj_path)

            if gt_tool_usage.get("any_call"):
                logger.error(
                    "CRITICAL: GT tool calls detected in Phase 2B for %s! "
                    "Total calls: %d. This should be zero.",
                    instance_id, gt_tool_usage.get("total_calls", 0),
                )

            try:
                with open(traj_path) as f:
                    traj_data = json.load(f)
                traj_data.setdefault("info", {})["gt_tool_usage"] = gt_tool_usage
                traj_data.setdefault("info", {})["test_execution"] = test_execution
                with open(traj_path, "w") as f:
                    json.dump(traj_data, f)
            except Exception:
                pass
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch
if _HAS_MINISWEAGENT:
    swebench_module.process_instance = gt_process_instance

if __name__ == "__main__":
    if not _HAS_MINISWEAGENT:
        import sys
        if "--help" in sys.argv or "-h" in sys.argv:
            print("run_mini_gt.py — Phase 2B: post-processing only (requires minisweagent)")
            sys.exit(0)
        print("ERROR: minisweagent is not installed. Install it first.", file=sys.stderr)
        sys.exit(1)
    app()
