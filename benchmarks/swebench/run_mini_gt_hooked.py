#!/usr/bin/env python3
"""Run mini-SWE-agent with GT v11 post-edit hook — Go indexer + ranked evidence.

v11 architecture:
  gt-index (Go binary) → graph.db (SQLite) → gt_intel.py (Python) → ranked evidence

The hook intercepts every command. If a source file is modified, GT runs
gt_intel.py to query the graph and produce ranked evidence (callers, tests,
siblings, impact). Output is appended to command stdout.

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
import subprocess
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

# v11: Go binary + Python intelligence layer
GT_INDEX_BINARY = Path(__file__).parent.parent.parent / "gt-index" / "gt-index-static"
GT_INTEL_SCRIPT = Path(__file__).parent / "gt_intel.py"

# Fallback: also keep gt_hook.py for environments where Go binary can't run
GT_HOOK_PATH = Path(__file__).parent / "gt_hook.py"

# Pre-encode gt_intel.py for injection (small file, single chunk)
_GT_INTEL_B64 = base64.b64encode(GT_INTEL_SCRIPT.read_bytes()).decode("ascii") if GT_INTEL_SCRIPT.exists() else ""

# Pre-encode Go binary for injection (larger, chunked)
_GT_INDEX_B64 = ""
_GT_INDEX_CHUNKS: list[str] = []
if GT_INDEX_BINARY.exists():
    _GT_INDEX_B64 = base64.b64encode(GT_INDEX_BINARY.read_bytes()).decode("ascii")
    _CHUNK_SIZE = 500_000  # 500KB chunks for the ~10MB binary
    _GT_INDEX_CHUNKS = [_GT_INDEX_B64[i:i + _CHUNK_SIZE] for i in range(0, len(_GT_INDEX_B64), _CHUNK_SIZE)]

# Fallback: gt_hook.py chunks (used if Go binary unavailable)
_GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii") if GT_HOOK_PATH.exists() else ""
_HOOK_CHUNK_SIZE = 50_000
_GT_HOOK_CHUNKS = [_GT_HOOK_B64[i:i + _HOOK_CHUNK_SIZE] for i in range(0, len(_GT_HOOK_B64), _HOOK_CHUNK_SIZE)] if _GT_HOOK_B64 else []

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


def _inject_v11(env, instance_id: str) -> bool:
    """Inject Go indexer binary + gt_intel.py via docker cp, build graph.db."""
    root = _detect_repo_root(env)
    _container_roots[env.container_id] = root
    use_go = GT_INDEX_BINARY.exists()
    container_id = env.container_id

    try:
        if use_go and container_id:
            # Use docker cp — go through the container's env.execute for validation first
            _exec(env, "echo gt_ready", timeout=5)  # verify container is alive

            import subprocess as _sp
            _sp.run(["docker", "cp", str(GT_INDEX_BINARY), f"{container_id}:/tmp/gt-index"],
                    timeout=15, check=True, capture_output=True)
            _sp.run(["docker", "cp", str(GT_INTEL_SCRIPT), f"{container_id}:/tmp/gt_intel.py"],
                    timeout=10, check=True, capture_output=True)
            _exec(env, "chmod +x /tmp/gt-index", timeout=5)

            # Build the graph index
            result = _exec(env, f"/tmp/gt-index --root={root} --output=/tmp/gt_graph.db --max-files=5000 2>&1", timeout=30)
            output = result.get("output", "") if isinstance(result, dict) else ""
            last_line = output.strip().split("\n")[-1][:100] if output else "no output"
            logger.info("v11 Go indexer: %s | %s", instance_id, last_line)
            return True

        else:
            # Fallback: inject gt_hook.py via base64 (v10 behavior)
            for i, chunk in enumerate(_GT_HOOK_CHUNKS):
                op = ">" if i == 0 else ">>"
                _exec(env, f"echo -n '{chunk}' {op} /tmp/gt_hook.b64", timeout=15)
            _exec(env, "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64", timeout=15)
            _exec(env, f"python3 /tmp/gt_hook.py understand /dev/null --root={root} --quiet --max-lines=1 2>/dev/null || true", timeout=40)
            logger.info("v10 fallback: gt_hook.py injected for %s (root=%s)", instance_id, root)
            return True

    except Exception as e:
        logger.warning("GT injection failed for %s: %s", instance_id, e)
        return False


_SOURCE_EXTS = r'\.(?:py|js|ts|jsx|tsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|swift|kt)'


def _is_repo_source(filepath: str) -> bool:
    """Filter out test scripts, temp files, and repro scripts."""
    base = os.path.basename(filepath)
    if base.startswith("test_") or base.startswith("reproduce") or base.startswith("tmp"):
        return False
    if "/test_" in filepath or "reproduce" in filepath:
        return False
    return True


def _detect_modified_file(command: str, output: str) -> str | None:
    """Heuristic: detect which source file a command modifies."""
    import re
    ext = _SOURCE_EXTS

    # Strategy 1: sed -i ... file.ext — find ALL source files in sed commands
    # Handles: sed -i 's/x/y/' file.py, sed -i '/pat/a text' ./lib/foo.py
    if "sed " in command and "-i" in command:
        # Find all source file paths in the command (relative or absolute)
        candidates = re.findall(rf'(\S+{ext})\b', command)
        # Filter: take the LAST source file that looks like repo code (not a pattern)
        for f in reversed(candidates):
            if _is_repo_source(f) and not f.startswith("'") and not f.startswith('"'):
                return f

    # Strategy 2: > file.ext or >> file.ext
    m = re.search(rf'>\s*(\S+{ext})', command)
    if m:
        f = m.group(1)
        if _is_repo_source(f):
            return f

    # Strategy 3: tee file.ext
    m = re.search(rf'tee\s+(\S+{ext})', command)
    if m:
        return m.group(1)

    # Strategy 4: patch file.ext
    m = re.search(rf'patch\s+.*?(\S+{ext})', command)
    if m:
        return m.group(1)

    # Strategy 5: cat > file.ext (heredoc)
    m = re.search(rf'cat\s*>\s*(\S+{ext})', command)
    if m:
        f = m.group(1)
        if _is_repo_source(f):
            return f

    # Strategy 6: Any source file path with edit indicators
    if any(ind in command for ind in _EDIT_INDICATORS):
        # Find ALL source files (relative with ./ or absolute with /)
        all_files = re.findall(rf'(\.?/\S+{ext})\b', command)
        repo_files = [f for f in all_files if _is_repo_source(f)]
        if repo_files:
            return repo_files[-1]

    return None


def _run_gt_intel(env, filepath: str) -> str:
    """Run gt_intel.py (v11) or gt_hook.py analyze (v10 fallback) on a file."""
    root = _container_roots.get(env.container_id, "/testbed")
    container_id = env.container_id

    seen = _seen_files.setdefault(container_id, set())
    if filepath in seen:
        return ""
    seen.add(filepath)

    # Normalize filepath to relative
    if filepath.startswith(root):
        rel_path = filepath[len(root):].lstrip("/")
    else:
        rel_path = filepath.lstrip("./")

    try:
        # v11: use gt_intel.py with graph.db from Go indexer
        if _GT_INDEX_CHUNKS:
            result = _exec(
                env,
                f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --file={rel_path} --root={root} 2>/dev/null",
                timeout=10,  # graph.db already built, queries are fast
            )
        else:
            # v10 fallback: use gt_hook.py analyze
            result = _exec(
                env,
                f"python3 /tmp/gt_hook.py analyze {filepath} --root={root} --quiet --max-lines=35 2>/dev/null",
                timeout=20,
            )

        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and len(output) > 30 and "Error" not in output[:30] and "Traceback" not in output[:50]:
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
    if not isinstance(command, str):
        return result

    # Only check for edits if command looks like it modifies files
    if any(ind in command for ind in _EDIT_INDICATORS):
        modified_file = _detect_modified_file(command, result.get("output", ""))
        if modified_file and self.container_id:
            gt_output = _run_gt_intel(self, modified_file)
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
        hook_ok = _inject_v11(env, instance_id)
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
                        "gt_version": "v11_go_indexer",
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
