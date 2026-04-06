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
import json
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
# v1.0.1: ego-graph module (sits alongside gt_intel.py)
# ego_graph.py lives in src/groundtruth/, not benchmarks/swebench/
GT_EGO_GRAPH = Path(__file__).parent.parent.parent / "src" / "groundtruth" / "ego_graph.py"
# v1.0.2: import_resolve.py kept as standalone diagnostic tool (not in pipeline)
# Resolution now happens inline during ego-graph BFS — no upfront processing

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

# v1.0.5: Injection budget — HARD CAP of 3 per task. Period.
# Injection 1: briefing (task start)
# Injection 2: first edit hook (first file modification)
# Injection 3: new-file hook (only if agent edits a DIFFERENT file)
# After 3: permanent silence.
_injection_counts: dict[str, int] = {}  # container_id -> count
_injected_files: dict[str, set] = {}  # container_id -> set of files that triggered hooks
MAX_INJECTIONS = 3

# Legacy counters kept for cleanup
_evidence_counts: dict[str, int] = {}
# v18: Store briefing target FILES (not just function names) for file-match filter
_briefing_target_files: dict[str, set[str]] = {}

# Store the repo root per container
_container_roots: dict[str, str] = {}

# v16: Store briefing-resolved target function names per container
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
            # v1.0.1: inject ego_graph.py for ego-graph briefing
            if GT_EGO_GRAPH.exists():
                _sp.run(["docker", "cp", str(GT_EGO_GRAPH), f"{container_id}:/tmp/ego_graph.py"],
                        timeout=10, check=True, capture_output=True)
            _exec(env, "chmod +x /tmp/gt-index", timeout=5)

            # Build the graph index
            max_files = os.environ.get("GT_MAX_FILES", "5000")
            result = _exec(env, f"/tmp/gt-index --root={root} --output=/tmp/gt_graph.db --max-files={max_files} 2>&1", timeout=30)
            output = result.get("output", "") if isinstance(result, dict) else ""
            last_line = output.strip().split("\n")[-1][:100] if output else "no output"
            logger.info("v11 Go indexer: %s | %s", instance_id, last_line)

            # v1.0.2: import resolution happens inline during ego-graph BFS
            # No upfront processing — ego_graph.py resolves name-match edges
            # on-the-fly via parse_imports_for_file() (~1ms/file, cached)

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
    """v1.0.5: Edit hook with HARD CAP of 3 injections per task.

    Injection budget:
      1. Briefing (task start) — counted separately in _generate_briefing
      2. First edit hook — fires ONCE on first file modification
      3. New-file hook — fires ONCE only if agent edits a DIFFERENT file

    After 3 total injections: permanent silence. No exceptions.
    BREAKING lines are stripped — replaced by single IMPACT line from gt_intel.
    """
    root = _container_roots.get(env.container_id, "/testbed")
    container_id = env.container_id

    # v1.0.5: HARD CAP — check injection budget
    injection_count = _injection_counts.get(container_id, 0)
    if injection_count >= MAX_INJECTIONS:
        return ""  # budget exhausted, permanent silence

    # Track edited files
    counts = _edit_counts.setdefault(container_id, {})
    counts[filepath] = counts.get(filepath, 0) + 1
    injected_files = _injected_files.setdefault(container_id, set())

    # v1.0.5: Only fire on FIRST edit of each file, skip re-edits of same file
    if filepath in injected_files:
        return ""  # already injected for this file, stay silent

    # Normalize filepath to relative
    if filepath.startswith(root):
        rel_path = filepath[len(root):].lstrip("/")
    else:
        rel_path = filepath.lstrip("./")

    try:
        if _GT_INDEX_CHUNKS:
            log_flag = "--log=/tmp/gt_evidence.jsonl"
            first_edit_flag = "--first-edit" if counts[filepath] == 1 else ""
            result = _exec(
                env,
                f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --file={rel_path} "
                f"--root={root} --edit-hook {first_edit_flag} {log_flag} 2>/dev/null",
                timeout=10,
            )
        else:
            result = _exec(
                env,
                f"python3 /tmp/gt_hook.py analyze {filepath} --root={root} --quiet --max-lines=35 2>/dev/null",
                timeout=20,
            )

        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and len(output) > 8 and "Error" not in output[:30] and "Traceback" not in output[:50]:
            # v1.0.5: Strip ALL BREAKING lines — replace with nothing.
            # The IMPACT line from gt_intel already summarizes caller count.
            lines = output.split("\n")
            lines = [l for l in lines if "BREAKING" not in l]
            output = "\n".join(lines).strip()
            if not output or len(output) < 8:
                return ""  # nothing left after stripping BREAKING

            # v1.0.5: Count this injection and mark the file
            _injection_counts[container_id] = injection_count + 1
            injected_files.add(filepath)
            _evidence_counts[container_id] = _evidence_counts.get(container_id, 0) + 1
            return f"\n\n{output}"
    except Exception:
        pass
    return ""


def _extract_briefing_targets(briefing_text: str) -> tuple[list[str], set[str]]:
    """v22: Extract target function names AND file paths from briefing output.
    Supports v22 (TARGET FILE + SKELETON), v21 (TARGET func), LIKELY FILES, SCOPE, ALSO."""
    import re
    targets = []
    target_files: set[str] = set()
    # v22 HIGH: TARGET FILE: path/to/file.py (conf)
    for match in re.finditer(r'TARGET FILE:\s*(\S+\.(?:py|go|js|ts|rs|java|rb|cpp|c|cs|kt|scala|swift|php))', briefing_text):
        target_files.add(match.group(1))
    # v22 SKELETON: extract function names from skeleton lines like "  func_name:123 (N callers)"
    for match in re.finditer(r'^\s+(\w+):\d+', briefing_text, re.MULTILINE):
        targets.append(match.group(1))
    # v21 backward compat: TARGET: func() at file:line (conf)
    for match in re.finditer(r'TARGET:\s*(\w[\w.]*)\(\)\s*at\s+(\S+?)(?::\d+)?\s', briefing_text):
        targets.append(match.group(1))
        target_files.add(match.group(2))
    # v21: ALSO: file1, file2
    for match in re.finditer(r'ALSO:\s*(.+)', briefing_text):
        for f in match.group(1).split(","):
            f = f.strip()
            if f and "/" in f:
                target_files.add(f)
    # v21-definitive MEDIUM: "LIKELY FILES:" + indented file lines
    for match in re.finditer(r'^\s+(\S+\.(?:py|go|js|ts|jsx|tsx|rs|java|rb|cpp|c|cs|kt|scala|swift|php))\s', briefing_text, re.MULTILINE):
        target_files.add(match.group(1))
    # v21-definitive LOW: "SCOPE:" + indented file lines
    for match in re.finditer(r'^\s+([\w/.-]+\.(?:py|go|js|ts|jsx|tsx|rs|java|rb|cpp|c|cs|kt|scala|swift|php))\s*$', briefing_text, re.MULTILINE):
        target_files.add(match.group(1))
    # Fallback: v19d FIX HERE format
    if not targets:
        for match in re.finditer(r'FIX HERE:\s*(\w+)\(\)\s*at\s+(\S+?)(?::\d+)?\s', briefing_text):
            targets.append(match.group(1))
            target_files.add(match.group(2))
    if not targets:
        for match in re.finditer(r'FIX HERE:\s*(\w+)\(\)', briefing_text):
            targets.append(match.group(1))
    return targets, target_files


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
            # v18: Extract target function names AND file paths for task-aware reminders + file-match filter
            targets, target_files = _extract_briefing_targets(output)
            if container_id:
                if targets:
                    _briefing_targets[container_id] = targets
                if target_files:
                    _briefing_target_files[container_id] = target_files
                # v1.0.5: count briefing as injection #1
                _injection_counts[container_id] = _injection_counts.get(container_id, 0) + 1
                _evidence_counts[container_id] = 1
                logger.info("v18 briefing for %s: targets=%s, files=%s", instance_id, targets, target_files)
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
    # v1.0.3: Live Lite images use starryzhang/ namespace — only for non-princeton datasets.
    # Standard SWE-bench Lite/Verified from princeton-nlp use swebench/ (mini-swe-agent default).
    # Disabled: the "/" in repo check was too broad (django/django has "/" too).
    # if "docker_image" not in instance and "/" in instance.get("repo", ""):
    #     id_docker = instance_id.replace("__", "_1776_")
    #     instance["docker_image"] = f"docker.io/starryzhang/sweb.eval.x86_64.{id_docker}:latest".lower()

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
            extra_info["briefing_text"] = briefing
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
            cid = getattr(env, "container_id", "")
            _edit_counts.pop(cid, None)
            _shown_files.pop(cid, None)
            _container_roots.pop(cid, None)
            _briefing_targets.pop(cid, None)
            _evidence_counts.pop(cid, None)
            _briefing_target_files.pop(cid, None)
            _injection_counts.pop(cid, None)
            _injected_files.pop(cid, None)

        # v1.0.4c: Write per-task GT artifact summary
        if env is not None:
            try:
                cid = getattr(env, "container_id", "")
                artifact = {
                    "instance_id": instance_id,
                    "gt_version": "v1.0.5",
                    "gt_delivery": "briefing+edit_hook+hard_cap_3_injections",
                    "briefing_fired": bool(extra_info.get("briefing_text")),
                    "edit_hook_count": sum(_edit_counts.get(cid, {}).values()),
                    "evidence_blocks_emitted": _evidence_counts.get(cid, 0),
                    "files_edited": list(_edit_counts.get(cid, {}).keys()),
                }
                artifact_path = instance_dir / "gt_artifact.json"
                artifact_path.write_text(json.dumps(artifact, indent=2))
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
                        "gt_version": "v1.0.5",
                        "gt_delivery": "briefing+edit_hook+hard_cap_3_injections",
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
