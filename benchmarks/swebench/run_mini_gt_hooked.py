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

# Commands that likely modify files — intentionally broad
_EDIT_INDICATORS = (
    "sed ", "cat >", "cat <<", "echo >", "echo >>",
    "tee ", "patch ", "git apply", ">>",
    "python -c", "python3 -c",
    "> ", ">> ",  # redirection operators
)

# v12: Track edit counts per file per container — fire GT on second edit, not first
_edit_counts: dict[str, dict[str, int]] = {}
# v17: Track which files had evidence shown — filepath → edit count when last shown
_shown_files: dict[str, dict[str, int]] = {}

# Store the repo root per container
_container_roots: dict[str, str] = {}

# v16: Store briefing-resolved target function names per container
# Used to pass task-aware function targeting to the post-edit reminder
_briefing_targets: dict[str, list[str]] = {}



def _detect_repo_root(env) -> str:
    """Detect repo root: /app for Pro, /testbed for Lite.
    v13: check /app/.git (not /app/lib) — works for all Pro repos."""
    try:
        import subprocess
        # Check for /app/.git first (Pro repos always have it)
        result = subprocess.run(
            ["docker", "exec", env.container_id, "test", "-d", "/app/.git"],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            return "/app"
        # Fallback: check /app exists at all
        result = subprocess.run(
            ["docker", "exec", env.container_id, "test", "-d", "/app"],
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
            max_files = os.environ.get("GT_MAX_FILES", "5000")
            result = _exec(env, f"/tmp/gt-index --root={root} --output=/tmp/gt_graph.db --max-files={max_files} 2>&1", timeout=30)
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
    """Detect which source file a command modifies. Broad matching — false positives
    are filtered by _is_repo_source and the dedup cache in _run_gt_intel."""
    import re
    ext = _SOURCE_EXTS

    # Find ALL source file paths mentioned in the command
    # Match: ./path/file.ext, path/file.ext, /abs/path/file.ext
    all_source_files = re.findall(rf'(\.?/?[\w/.-]+{ext})\b', command)

    # Filter to repo source files only (not test scripts, temp files)
    repo_files = [f for f in all_source_files
                  if _is_repo_source(f)
                  and not f.startswith("'") and not f.startswith('"')
                  and len(f) > 5]  # skip very short matches

    if not repo_files:
        return None

    # For sed/patch/cat>/>> commands: return the LAST repo file (usually the target)
    if any(ind in command for ind in ("sed ", "patch ", "> ", ">> ", "cat >", "cat >>")):
        return repo_files[-1]

    # For other edit indicators: also return last repo file
    if any(ind in command for ind in _EDIT_INDICATORS):
        return repo_files[-1]

    return None


def _run_gt_intel(env, filepath: str) -> str:
    """Run gt_intel.py (v16) with task-aware function targeting."""
    root = _container_roots.get(env.container_id, "/testbed")
    container_id = env.container_id

    # v17: track edit counts — fire on 2nd edit per file
    # M11 fix: allow re-fire if file was edited again since last evidence
    counts = _edit_counts.setdefault(container_id, {})
    shown = _shown_files.setdefault(container_id, {})  # filepath → edit_count_when_shown
    counts[filepath] = counts.get(filepath, 0) + 1
    if counts[filepath] < 2:
        return ""  # suppress first edit (often exploration)
    shown_at = shown.get(filepath, 0)
    if shown_at >= counts[filepath]:
        return ""  # no new edits since last evidence
    shown[filepath] = counts[filepath]

    # Normalize filepath to relative
    if filepath.startswith(root):
        rel_path = filepath[len(root):].lstrip("/")
    else:
        rel_path = filepath.lstrip("./")

    try:
        # v16: use briefing-resolved targets for task-aware function selection
        func_flag = ""
        targets = _briefing_targets.get(container_id, [])
        if targets:
            func_flag = f"--function={targets[0]}"

        # v11: use gt_intel.py with graph.db from Go indexer
        if _GT_INDEX_CHUNKS:
            log_flag = "--log=/tmp/gt_evidence.jsonl"
            result = _exec(
                env,
                f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --file={rel_path} {func_flag} "
                f"--root={root} --reminder {log_flag} 2>/dev/null",
                timeout=10,
            )
        else:
            # v10 fallback: use gt_hook.py analyze
            result = _exec(
                env,
                f"python3 /tmp/gt_hook.py analyze {filepath} --root={root} --quiet --max-lines=35 2>/dev/null",
                timeout=20,
            )

        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and len(output) > 8 and "Error" not in output[:30] and "Traceback" not in output[:50]:
            return f"\n\n{output}"
    except Exception:
        pass
    return ""


def _extract_briefing_targets(briefing_text: str) -> list[str]:
    """v16: Extract target function names from briefing output for task-aware reminders."""
    import re
    targets = []
    for match in re.finditer(r'FIX HERE:\s*(\w+)\(\)', briefing_text):
        targets.append(match.group(1))
    return targets


def _generate_briefing(env, task_text: str, instance_id: str) -> str:
    """v16: Enhanced pre-task briefing — graph evidence before the PR description.
    Also stores resolved target symbols for task-aware post-edit reminders."""
    root = _container_roots.get(getattr(env, "container_id", ""), "/testbed")
    container_id = getattr(env, "container_id", "")
    try:
        # Write issue text to container (escape single quotes)
        safe_text = task_text[:5000].replace("'", "'\\''")
        _exec(env, f"echo '{safe_text}' > /tmp/issue.txt", timeout=5)

        result = _exec(
            env,
            f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --enhanced-briefing "
            f"--issue-text=@/tmp/issue.txt --root={root} 2>/dev/null",
            timeout=20,
        )
        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and ("CODEBASE CONTEXT" in output or "<gt-evidence>" in output) and len(output) > 30:
            logger.info("v16 enhanced briefing for %s: %d lines", instance_id, output.count("\n") + 1)
            # v16: Extract and store target function names for task-aware reminders
            targets = _extract_briefing_targets(output)
            if targets and container_id:
                _briefing_targets[container_id] = targets
                logger.info("v16 briefing targets for %s: %s", instance_id, targets)
            return output
    except Exception as e:
        logger.warning("v16 briefing failed for %s: %s", instance_id, e)
    return ""


# ── Monkey-patch DockerEnvironment.execute ──────────────────────────────
_original_execute = DockerEnvironment.execute


def _hooked_execute(self, action, cwd="", *, timeout=None):
    """Execute command, then check for modified source files via git status."""
    root = _container_roots.get(getattr(self, "container_id", ""), "/testbed")

    result = _original_execute(self, action, cwd=cwd, timeout=timeout)

    command = action.get("command", "") if isinstance(action, dict) else ""
    if not isinstance(command, str) or not getattr(self, "container_id", None):
        return result

    # Skip read-only commands (grep, cat, find, ls, head, tail, etc.)
    first_word = command.strip().split()[0] if command.strip() else ""
    readonly = {"grep", "cat", "find", "ls", "head", "tail", "wc", "diff", "git",
                "python3", "python", "echo", "cd", "pwd", "which", "pip", "pip3",
                "apt", "apt-get", "conda", "test", "file", "stat", "du", "df"}
    if first_word in readonly and ">" not in command and ">>" not in command:
        return result

    # After every non-readonly command: check git status for modified source files
    try:
        check = _original_execute(
            self,
            {"command": f"cd {root} && git diff --name-only 2>/dev/null | head -5"},
            cwd=root, timeout=5,
        )
        diff_output = check.get("output", "") if isinstance(check, dict) else ""
        if diff_output.strip():
            for line in diff_output.strip().split("\n"):
                fpath = line.strip()
                if not fpath:
                    continue
                # Check if it's a source file we haven't analyzed yet
                ext = os.path.splitext(fpath)[1]
                if ext in {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
                           ".rb", ".php", ".c", ".cpp", ".h", ".cs", ".cjs", ".mjs"}:
                    if _is_repo_source(fpath):
                        gt_output = _run_gt_intel(self, fpath)
                        if gt_output:
                            result["output"] = result.get("output", "") + gt_output
                            break  # one file per command is enough
    except Exception:
        pass

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

        # v12: Pre-task briefing — query graph for symbols mentioned in issue
        briefing = _generate_briefing(env, task, instance_id)
        if briefing:
            task = briefing + "\n\n" + task
            extra_info["briefing_shown"] = True
            extra_info["briefing_lines"] = briefing.count("\n") + 1
            container_id = getattr(env, "container_id", "")
            extra_info["briefing_targets"] = _briefing_targets.get(container_id, [])
        else:
            extra_info["briefing_shown"] = False

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
        # Extract hook logs + v12 evidence logs
        if env is not None:
            try:
                log_dir = output_dir / "gt_logs"
                log_dir.mkdir(exist_ok=True)
                # v10/v11 hook log
                log_result = _exec(env, "cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''", timeout=10)
                log_content = log_result.get("output", "") if isinstance(log_result, dict) else ""
                if log_content.strip():
                    (log_dir / f"{instance_id}.jsonl").write_text(log_content)
                # v12 evidence log (per-evidence-event JSONL)
                ev_result = _exec(env, "cat /tmp/gt_evidence.jsonl 2>/dev/null || echo ''", timeout=10)
                ev_content = ev_result.get("output", "") if isinstance(ev_result, dict) else ""
                if ev_content.strip():
                    (log_dir / f"{instance_id}.evidence.jsonl").write_text(ev_content)
            except Exception:
                pass

            # Clean up per-container state
            _edit_counts.pop(getattr(env, "container_id", ""), None)
            _shown_files.pop(getattr(env, "container_id", ""), None)
            _container_roots.pop(getattr(env, "container_id", ""), None)
            _briefing_targets.pop(getattr(env, "container_id", ""), None)

        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "v16_task_aware_targeting",
                        "gt_delivery": "enhanced_briefing_plus_reminder_hook",
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
