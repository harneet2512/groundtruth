#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with Phase 3 pattern-aware GT tools.

Architecture: The agent has 3 GT analysis commands available during work:
1. groundtruth_references <Symbol> — find usages + definition pointer
2. groundtruth_impact <Symbol> — obligation sites, conventions, subclass overrides
3. groundtruth_check — completeness check against git diff

Plus post-processing autocorrect (belt & suspenders, expect zero corrections)
and bounded test feedback (agent behavior, not a GT tool).

Phase 3: Pattern-aware obligations + A/B evaluation.
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

# Path to the GT tool script
GT_TOOL_PATH = Path(__file__).parent / "gt_tool.py"
_GT_TOOL_B64 = base64.b64encode(GT_TOOL_PATH.read_bytes()).decode("ascii")

# Path to the autocorrect engine
GT_AUTOCORRECT_PATH = Path(__file__).parent / "gt_autocorrect.py"
_GT_AUTOCORRECT_B64 = base64.b64encode(GT_AUTOCORRECT_PATH.read_bytes()).decode("ascii")

# Path to the runtime introspection KB builder
GT_RUNTIME_KB_PATH = Path(__file__).parent / "gt_runtime_kb.py"
_GT_RUNTIME_KB_B64 = base64.b64encode(GT_RUNTIME_KB_PATH.read_bytes()).decode("ascii")


def _exec(env, cmd: str, timeout: int = 60) -> dict:
    """Execute a command in the environment, handling the dict action format."""
    return env.execute({"command": cmd}, timeout=timeout)


def _setup_phase3(env, instance_id: str) -> dict:
    """Set up Phase 3 tools in the container.

    Copies gt_tool.py (with Phase 3 commands), autocorrect, and runtime KB scripts.
    Pre-warms the index via help command.
    """
    setup_result = {
        "gt_tool_available": False,
        "postprocessing_available": False,
        "runtime_kb_built": False,
        "runtime_kb_stats": {},
        "index_prewarm": False,
    }

    try:
        # Copy gt_tool.py into container
        _exec(env, f"echo '{_GT_TOOL_B64}' | base64 -d > /tmp/gt_tool.py")
        setup_result["gt_tool_available"] = True

        # Pre-warm index
        try:
            warmup = _exec(
                env,
                "cd /testbed && python3 /tmp/gt_tool.py help 2>/dev/null | tail -1",
                timeout=45,
            )
            setup_result["index_prewarm"] = True
            logger.info("GT index pre-warmed for %s", instance_id)
        except Exception as e:
            logger.warning("GT index pre-warm failed for %s: %s", instance_id, e)

        # Copy autocorrect engine (post-processing belt & suspenders)
        _exec(env, f"echo '{_GT_AUTOCORRECT_B64}' | base64 -d > /tmp/gt_autocorrect.py")

        # Copy runtime KB builder
        _exec(env, f"echo '{_GT_RUNTIME_KB_B64}' | base64 -d > /tmp/gt_runtime_kb.py")

        setup_result["postprocessing_available"] = True

        # Build runtime KB during setup (before agent starts)
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
                # Save runtime KB to disk
                import base64 as b64mod
                kb_b64 = b64mod.b64encode(kb_output.encode()).decode("ascii")
                _exec(
                    env,
                    f"echo '{kb_b64}' | base64 -d > /tmp/gt_runtime_kb.json",
                    timeout=5,
                )
                logger.info(
                    "Runtime KB built for %s: %d classes, %d methods",
                    instance_id,
                    kb_data.get("total_classes", 0),
                    kb_data.get("total_methods", 0),
                )
            except (json.JSONDecodeError, ValueError):
                logger.warning("Runtime KB output not valid JSON for %s", instance_id)
        except Exception as e:
            logger.warning("Runtime KB build failed for %s: %s", instance_id, e)

    except Exception as e:
        logger.warning("Phase 3 setup error for %s: %s", instance_id, e)
        setup_result["error"] = str(e)

    return setup_result


def _check_gt_tool_usage(traj_path: Path) -> dict:
    """Scan saved trajectory for gt_tool.py invocations (Phase 3 commands)."""
    usage: dict = {
        "any_call": False,
        "total_calls": 0,
        "first_call_turn": None,
        "commands_used": [],
        "symbols_queried": [],
        "total_turns": 0,
        "command_counts": {},
        "call_turns": [],
        "workflow_compliance": True,
    }

    gt_pattern = re.compile(
        r"gt_tool\.py\s+(groundtruth_references|groundtruth_impact|groundtruth_check|"
        r"references|outline|impact|diagnose|check|help|search|scope|obligations|context|related|summary|diff)"
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
        command_sequence: list[str] = []

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
                command_sequence.append(cmd)
                if match.group(2):
                    symbols_seen.add(match.group(2))

        usage["commands_used"] = sorted(commands_seen)
        usage["symbols_queried"] = sorted(symbols_seen)
        usage["command_counts"] = command_counts
        usage["call_turns"] = call_turns
        usage["command_sequence"] = command_sequence
        if call_turns:
            usage["last_call_turn"] = call_turns[-1]
            usage["call_density"] = len(call_turns) / max(len(messages), 1)

        # Workflow compliance: check if agent used >4 GT calls (excessive)
        if usage["total_calls"] > 4:
            usage["workflow_compliance"] = False

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


def _generate_gt_report(traj_path: Path, gt_usage: dict) -> None:
    """Save .gt_report.json alongside trajectory."""
    report_path = traj_path.with_suffix(".gt_report.json")
    report = {
        "total_gt_calls": gt_usage.get("total_calls", 0),
        "command_counts": gt_usage.get("command_counts", {}),
        "symbols_queried": gt_usage.get("symbols_queried", []),
        "command_sequence": gt_usage.get("command_sequence", []),
        "workflow_compliance": gt_usage.get("workflow_compliance", True),
        "total_turns": gt_usage.get("total_turns", 0),
        "first_call_turn": gt_usage.get("first_call_turn"),
        "last_call_turn": gt_usage.get("last_call_turn"),
        "call_density": gt_usage.get("call_density", 0),
    }
    try:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
    except Exception:
        pass


def gt_process_instance_phase3(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Phase 3: Agent has 3 GT tools + test feedback. Post-processing as belt & suspenders."""
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
    gt_setup = {"gt_tool_available": False, "postprocessing_available": False}

    try:
        env = get_sb_environment(config, instance)

        # --- PHASE 3 SETUP (GT tools + post-processing) ---
        progress_manager.update_instance_status(instance_id, "GT: Phase 3 setup")
        gt_setup = _setup_phase3(env, instance_id)

        task = original_task  # NEVER modify the problem statement

        # --- RUN AGENT (GT tools available) ---
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

        # --- POST-PROCESSING AUTOCORRECT (belt & suspenders) ---
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
                        "gt_version": "phase3",
                        "gt_delivery": "3_tools_during_work",
                        "gt_tool_available": gt_setup.get("gt_tool_available", False),
                        "gt_index_prewarmed": gt_setup.get("index_prewarm", False),
                        "gt_postprocessing_available": gt_setup.get("postprocessing_available", False),
                        "gt_runtime_kb_built": gt_setup.get("runtime_kb_built", False),
                        "gt_runtime_kb_stats": gt_setup.get("runtime_kb_stats", {}),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)

            # Post-save: scan trajectory for GT tool usage
            gt_tool_usage = _check_gt_tool_usage(traj_path)
            test_execution = _check_test_execution(traj_path)

            # Generate GT report
            _generate_gt_report(traj_path, gt_tool_usage)

            logger.info(
                "Phase 3 GT usage for %s: %d calls, commands=%s, symbols=%s, compliant=%s",
                instance_id,
                gt_tool_usage.get("total_calls", 0),
                gt_tool_usage.get("commands_used", []),
                gt_tool_usage.get("symbols_queried", []),
                gt_tool_usage.get("workflow_compliance", True),
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
    swebench_module.process_instance = gt_process_instance_phase3

if __name__ == "__main__":
    if not _HAS_MINISWEAGENT:
        import sys
        if "--help" in sys.argv or "-h" in sys.argv:
            print("run_mini_phase3.py — Phase 3: pattern-aware GT tools (requires minisweagent)")
            sys.exit(0)
        print("ERROR: minisweagent is not installed. Install it first.", file=sys.stderr)
        sys.exit(1)
    app()
