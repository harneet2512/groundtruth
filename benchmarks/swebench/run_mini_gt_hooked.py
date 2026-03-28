#!/usr/bin/env python3
"""Run mini-SWE-agent with GT v10 post-edit hook — automatic ego-graph feedback.

The hook intercepts every command the agent runs. If the command modifies a .py
file, GT automatically runs understand on the modified file and appends the
ego-graph output to the command's stdout. The agent sees cross-file context
as part of normal command output — like a compiler warning.

Zero extra calls. Zero prompt injection. The agent just works and GT feedback
appears when relevant.

Works with both SWE-bench Lite (/testbed) and Pro (/app).

Usage:
    python run_mini_gt_hooked.py \
        -c benchmarks/swebench/mini_swebench_pro_baseline.yaml \
        --model openai/qwen3-coder \
        --subset ScaleAI/SWE-bench_Pro --split test --slice 0:5 -w 2
"""
from __future__ import annotations

import base64
import os
import traceback
from pathlib import Path

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
from minisweagent.environments.docker import DockerEnvironment

GT_HOOK_PATH = Path(__file__).parent / "gt_hook.py"
_GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii")
_CHUNK_SIZE = 50_000
_CHUNKS = [_GT_HOOK_B64[i:i + _CHUNK_SIZE] for i in range(0, len(_GT_HOOK_B64), _CHUNK_SIZE)]

# Commands that likely modify files
_EDIT_INDICATORS = (
    "sed -i", "sed -e", "cat >", "cat <<", "echo >", "echo >>",
    "tee ", "mv ", "cp ", "patch ", "git apply", ">>",
    "python -c", "python3 -c",  # inline scripts that may write files
)

# Track which files we've already shown ego-graph for (per container)
_seen_files: dict[str, set[str]] = {}

# Store the repo root per container
_container_roots: dict[str, str] = {}


def _detect_repo_root(env) -> str:
    """Detect repo root: /app for Pro, /testbed for Lite."""
    try:
        import subprocess
        result = subprocess.run(
            ["docker", "exec", env.container_id, "test", "-d", "/app/lib"],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            return "/app"
    except Exception:
        pass
    return "/testbed"


def _exec(env, cmd: str, timeout: int = 60):
    return env.execute({"command": cmd}, timeout=timeout)


def _inject_hook(env, instance_id: str) -> bool:
    """Inject gt_hook.py into container."""
    try:
        for i, chunk in enumerate(_CHUNKS):
            op = ">" if i == 0 else ">>"
            _exec(env, f"echo -n '{chunk}' {op} /tmp/gt_hook.b64", timeout=15)
        _exec(env, "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64", timeout=15)

        # Pre-build the index (runs once, cached for subsequent calls)
        root = _detect_repo_root(env)
        _container_roots[env.container_id] = root
        logger.info("gt_hook.py injected: %s (root=%s)", instance_id, root)

        # Trigger index build in background — don't block
        _exec(env, f"python3 /tmp/gt_hook.py understand /dev/null --root={root} --quiet --max-lines=1 2>/dev/null || true", timeout=40)
        logger.info("GT index pre-built for %s", instance_id)
        return True
    except Exception as e:
        logger.warning("gt_hook injection failed for %s: %s", instance_id, e)
        return False


def _detect_modified_py_file(command: str, output: str) -> str | None:
    """Heuristic: detect which .py file a command modifies."""
    # Look for .py files mentioned in the command
    import re
    # Direct file reference in edit commands
    for pattern in [
        r'sed\s+-i[^\s]*\s+[^\s]*\s+(\S+\.py)',  # sed -i 's/x/y/' file.py
        r'>\s*(\S+\.py)',                            # > file.py or >> file.py
        r'tee\s+(\S+\.py)',                          # tee file.py
        r'cat\s*>\s*(\S+\.py)',                      # cat > file.py
        r'patch\s+.*?(\S+\.py)',                     # patch file.py
    ]:
        m = re.search(pattern, command)
        if m:
            return m.group(1)

    # If command looks like it creates/modifies a file via heredoc or echo redirect
    if any(ind in command for ind in ("cat <<", "python3 -c", "python -c")):
        # Check for .py file path in the command
        py_files = re.findall(r'(\S+\.py)\b', command)
        if py_files:
            return py_files[-1]  # last mentioned .py file

    return None


def _run_gt_hook(env, filepath: str) -> str:
    """Run gt_hook.py understand on a file and return the output."""
    root = _container_roots.get(env.container_id, "/testbed")
    container_id = env.container_id

    # Don't re-analyze files we've already shown
    seen = _seen_files.setdefault(container_id, set())
    if filepath in seen:
        return ""
    seen.add(filepath)

    try:
        result = _exec(
            env,
            f"python3 /tmp/gt_hook.py analyze {filepath} --root={root} --quiet --max-lines=35 2>/dev/null",
            timeout=20,  # index is pre-built; analyze runs 3 signals
        )
        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and len(output) > 30 and "Error" not in output[:30]:
            return f"\n\n{output}"
    except Exception:
        pass
    return ""


# ── Monkey-patch DockerEnvironment.execute ──────────────────────────────
_original_execute = DockerEnvironment.execute


def _hooked_execute(self, action, cwd="", *, timeout=None):
    """Execute command, then append GT ego-graph if a .py file was modified."""
    result = _original_execute(self, action, cwd=cwd, timeout=timeout)

    command = action.get("command", "") if isinstance(action, dict) else ""

    # Only check for edits if command looks like it modifies files
    if any(ind in command for ind in _EDIT_INDICATORS):
        modified_file = _detect_modified_py_file(command, result.get("output", ""))
        if modified_file and self.container_id:
            gt_output = _run_gt_hook(self, modified_file)
            if gt_output:
                result["output"] = result.get("output", "") + gt_output

    return result


DockerEnvironment.execute = _hooked_execute


# ── Process instance (same as baseline — no precompute, hook handles it) ──

def hooked_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Process instance with GT hook — GT context appears automatically after edits."""
    instance_id = instance["instance_id"]
    # Map Pro dockerhub_tag
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
    env = None
    exit_status = None
    result = None
    extra_info: dict = {}

    try:
        env = get_sb_environment(config, instance)

        # Inject gt_hook.py and pre-build index
        progress_manager.update_instance_status(instance_id, "GT: injecting hook + building index")
        hook_ok = _inject_hook(env, instance_id)
        extra_info["hook_injected"] = hook_ok

        # Run agent — GT hook fires automatically after file edits
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
    finally:
        # Extract hook logs
        if env is not None:
            try:
                log_result = _exec(env, "cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''", timeout=10)
                log_content = log_result.get("output", "") if isinstance(log_result, dict) else ""
                if log_content.strip():
                    log_dir = output_dir / "gt_logs"
                    log_dir.mkdir(exist_ok=True)
                    (log_dir / f"{instance_id}.jsonl").write_text(log_content)
            except Exception:
                pass

            # Clean up seen files for this container
            _seen_files.pop(getattr(env, "container_id", ""), None)
            _container_roots.pop(getattr(env, "container_id", ""), None)

        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "v10_hooked",
                        "gt_delivery": "post_edit_hook",
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )

        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


swebench_module.process_instance = hooked_process_instance

if __name__ == "__main__":
    app()
