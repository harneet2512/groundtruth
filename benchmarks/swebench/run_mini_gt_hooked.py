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

# vNext: Per-container fingerprint set for novelty suppression
_novelty_seen: dict[str, set[str]] = {}

# vNext: Per-container review_patch state
# Tracks whether review_patch has fired since the last edit.
# Reset when a new edit is detected so the agent gets a fresh review.
_review_state: dict[str, dict] = {}
# schema: {container_id: {"fired": bool, "edit_cycle": int, "findings_count": int}}



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


def _novelty_fingerprint(finding: dict) -> str:
    """Host-side fingerprint for novelty suppression of Finding dicts."""
    loc = finding.get("location", {})
    return f"{finding.get('kind', '')}|{loc.get('file', '')}|{loc.get('line', '')}|{loc.get('symbol', '')}"


def _filter_novel_findings(container_id: str, findings: list[dict]) -> list[dict]:
    """Drop findings already shown to this container."""
    seen = _novelty_seen.setdefault(container_id, set())
    novel = []
    for f in findings:
        fp = _novelty_fingerprint(f)
        if fp not in seen:
            seen.add(fp)
            novel.append(f)
    return novel


def _run_gt_intel(env, filepath: str) -> str:
    """Run gt_intel.py with findings-json mode and host-side novelty."""
    root = _container_roots.get(env.container_id, "/testbed")
    container_id = env.container_id

    # v17: track edit counts — fire on 2nd edit per file
    counts = _edit_counts.setdefault(container_id, {})
    shown = _shown_files.setdefault(container_id, {})
    counts[filepath] = counts.get(filepath, 0) + 1
    if counts[filepath] < 2:
        return ""
    shown_at = shown.get(filepath, 0)
    if shown_at >= counts[filepath]:
        return ""
    shown[filepath] = counts[filepath]

    # Normalize filepath to relative
    if filepath.startswith(root):
        rel_path = filepath[len(root):].lstrip("/")
    else:
        rel_path = filepath.lstrip("./")

    try:
        func_flag = ""
        targets = _briefing_targets.get(container_id, [])
        if targets:
            func_flag = f"--function={targets[0]}"

        if _GT_INDEX_CHUNKS:
            log_flag = "--log=/tmp/gt_evidence.jsonl"
            # vNext: use --findings-json for structured output
            result = _exec(
                env,
                f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --file={rel_path} {func_flag} "
                f"--root={root} --findings-json --surface=event_brief {log_flag} 2>/dev/null",
                timeout=10,
            )
            output = result.get("output", "").strip() if isinstance(result, dict) else ""
            if output and output.startswith("["):
                import json
                try:
                    findings = json.loads(output)
                    if findings:
                        # Host-side novelty: drop already-shown findings
                        novel = _filter_novel_findings(container_id, findings)
                        if novel:
                            # Format as surface-tagged text
                            lines = ['<gt-evidence surface="event_brief">']
                            for f in novel:
                                tier = f.get("tier", "INFO")
                                kind = f.get("kind", "")
                                msg = f.get("message", "")
                                loc = f.get("location", {})
                                loc_s = f"{loc.get('file', '')}:{loc.get('line', '')}" if loc.get("line") else loc.get("file", "")
                                conf = f.get("confidence", 0)
                                action = f.get("agent_action", "verify").upper().replace("_", " ")
                                lines.append(f"[{tier}] [{kind}] {msg} @ {loc_s} ({conf:.2f}) — {action}")
                            lines.append("</gt-evidence>")
                            return "\n\n" + "\n".join(lines)
                        return ""  # all findings already shown — silent
                except (json.JSONDecodeError, TypeError):
                    pass
            # Fallback: non-JSON output (old gt_intel.py or error)
            if output and len(output) > 8 and "Error" not in output[:30] and "Traceback" not in output[:50]:
                return f"\n\n{output}"
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
    """vNext task_map surface: pre-task localization via findings-json.

    Calls gt_intel.py --enhanced-briefing --findings-json to get structured
    findings, then formats them as surface-tagged text and seeds the
    host-side novelty set so event_brief won't repeat them.

    Falls back to old enhanced-briefing text if --findings-json fails.
    """
    root = _container_roots.get(getattr(env, "container_id", ""), "/testbed")
    container_id = getattr(env, "container_id", "")
    try:
        safe_text = task_text[:5000].replace("'", "'\\''")
        _exec(env, f"echo '{safe_text}' > /tmp/issue.txt", timeout=5)

        # vNext path: try --findings-json for structured output
        if _GT_INDEX_CHUNKS:
            result = _exec(
                env,
                f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --enhanced-briefing "
                f"--issue-text=@/tmp/issue.txt --root={root} --findings-json "
                f"--surface=task_map 2>/dev/null",
                timeout=20,
            )
            output = result.get("output", "").strip() if isinstance(result, dict) else ""

            # Try to parse as findings JSON
            if output and output.startswith("["):
                import json
                try:
                    findings = json.loads(output)
                    if findings:
                        # Seed novelty with task_map findings
                        novel = _filter_novel_findings(container_id, findings)
                        # Extract targets from findings
                        targets = []
                        for f in findings:
                            sym = f.get("location", {}).get("symbol")
                            if sym and sym not in targets:
                                targets.append(sym)
                        if targets and container_id:
                            _briefing_targets[container_id] = targets
                            logger.info("vNext task_map targets for %s: %s", instance_id, targets)
                        # Format as surface-tagged text
                        lines = ['<gt-evidence surface="task_map">']
                        for f in novel:
                            tier = f.get("tier", "INFO")
                            kind = f.get("kind", "")
                            msg = f.get("message", "")
                            loc = f.get("location", {})
                            loc_s = f"{loc.get('file', '')}:{loc.get('line', '')}" if loc.get("line") else loc.get("file", "")
                            conf = f.get("confidence", 0)
                            action = f.get("agent_action", "verify").upper().replace("_", " ")
                            lines.append(f"[{tier}] [{kind}] {msg} @ {loc_s} ({conf:.2f}) — {action}")
                        lines.append("</gt-evidence>")
                        text = "\n".join(lines)
                        logger.info("vNext task_map for %s: %d findings", instance_id, len(novel))
                        return text
                except (json.JSONDecodeError, TypeError):
                    pass

        # Fallback: old enhanced-briefing text path
        result = _exec(
            env,
            f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --enhanced-briefing "
            f"--issue-text=@/tmp/issue.txt --root={root} 2>/dev/null",
            timeout=20,
        )
        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and ("CODEBASE CONTEXT" in output or "<gt-evidence>" in output) and len(output) > 30:
            logger.info("vNext task_map fallback for %s: %d lines", instance_id, output.count("\n") + 1)
            targets = _extract_briefing_targets(output)
            if targets and container_id:
                _briefing_targets[container_id] = targets
            if container_id:
                _novelty_seen.setdefault(container_id, set())
            return output
    except Exception as e:
        logger.warning("vNext task_map failed for %s: %s", instance_id, e)
    return ""


def _run_review_patch(env, instance_id: str) -> str:
    """vNext review_patch surface: pre-submit deterministic diff review.

    Runs gt_intel.py on each modified file with --findings-json, collects
    all findings, applies host-side novelty, and returns binding-aware text.
    """
    root = _container_roots.get(getattr(env, "container_id", ""), "/testbed")
    container_id = getattr(env, "container_id", "")
    try:
        # Get modified files from git diff
        check = _exec(env, f"cd {root} && git diff --name-only 2>/dev/null", timeout=5)
        diff_output = check.get("output", "") if isinstance(check, dict) else ""
        if not diff_output.strip():
            return ""

        all_findings: list[dict] = []
        source_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
                       ".rb", ".php", ".c", ".cpp", ".h", ".cs"}
        for line in diff_output.strip().split("\n"):
            fpath = line.strip()
            if not fpath or not _is_repo_source(fpath):
                continue
            ext = os.path.splitext(fpath)[1]
            if ext not in source_exts:
                continue

            # Run gt_intel.py on each modified file
            if _GT_INDEX_CHUNKS:
                result = _exec(
                    env,
                    f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --file={fpath} "
                    f"--root={root} --findings-json --surface=review_patch 2>/dev/null",
                    timeout=10,
                )
                output = result.get("output", "").strip() if isinstance(result, dict) else ""
                if output and output.startswith("["):
                    import json
                    try:
                        findings = json.loads(output)
                        all_findings.extend(findings)
                    except (json.JSONDecodeError, TypeError):
                        pass

        if not all_findings:
            return ""

        # Host-side novelty: drop findings already shown during event_brief
        novel = _filter_novel_findings(container_id, all_findings)
        if not novel:
            return ""

        # Format as review_patch surface with binding awareness
        lines = ['<gt-evidence surface="review_patch">']
        fix_required_count = 0
        for f in novel:
            tier = f.get("tier", "INFO")
            kind = f.get("kind", "")
            msg = f.get("message", "")
            loc = f.get("location", {})
            loc_s = f"{loc.get('file', '')}:{loc.get('line', '')}" if loc.get("line") else loc.get("file", "")
            conf = f.get("confidence", 0)
            action = f.get("agent_action", "verify").upper().replace("_", " ")
            lines.append(f"[{tier}] [{kind}] {msg} @ {loc_s} ({conf:.2f}) — {action}")
            if conf >= 0.85:
                fix_required_count += 1
        if fix_required_count > 0:
            lines.append("---")
            lines.append(f"BINDING: {fix_required_count} finding(s) require explicit fix or ACK before submit.")
        lines.append("</gt-evidence>")
        text = "\n".join(lines)
        logger.info("vNext review_patch for %s: %d novel findings, %d binding",
                     instance_id, len(novel), fix_required_count)
        return text
    except Exception as e:
        logger.warning("vNext review_patch failed for %s: %s", instance_id, e)
    return ""


# ── Monkey-patch DockerEnvironment.execute ──────────────────────────────
_original_execute = DockerEnvironment.execute


def _has_uncommitted_edits(env, root: str) -> bool:
    """Check if the container has uncommitted source file edits."""
    try:
        check = _original_execute(
            env,
            {"command": f"cd {root} && git diff --name-only 2>/dev/null | head -3"},
            cwd=root, timeout=5,
        )
        output = check.get("output", "") if isinstance(check, dict) else ""
        return bool(output.strip())
    except Exception:
        return False


def _is_git_review_command(command: str) -> bool:
    """Return True if the command is the agent reviewing its work before submit."""
    cmd = command.strip()
    if cmd.startswith("git diff") or cmd.startswith("git status"):
        return True
    if cmd == "git log" or cmd.startswith("git log "):
        return True
    return False


def _is_submit_command(command: str) -> bool:
    """Return True if the command signals the agent is submitting."""
    cmd = command.strip().lower()
    if cmd in ("submit", "submit_patch", "exit"):
        return True
    if "complete_task_and_submit" in cmd:
        return True
    if "submit_final_output" in cmd:
        return True
    return False


def _hooked_execute(self, action, cwd="", *, timeout=None):
    """Execute command with GT hooks: event_brief + review_patch surfaces."""
    root = _container_roots.get(getattr(self, "container_id", ""), "/testbed")
    container_id = getattr(self, "container_id", "")

    result = _original_execute(self, action, cwd=cwd, timeout=timeout)

    command = action.get("command", "") if isinstance(action, dict) else ""
    if not isinstance(command, str) or not container_id:
        return result

    first_word = command.strip().split()[0] if command.strip() else ""

    # ── review_patch: intercept git review commands BEFORE readonly skip ──
    # When the agent runs `git diff`, `git status`, or `submit` after having
    # made edits, fire review_patch. This is the pre-submit review point —
    # the agent sees findings and can fix or ACK before deciding to submit.
    if (_is_git_review_command(command) or _is_submit_command(command)):
        state = _review_state.get(container_id, {"fired": False, "edit_cycle": 0})
        edit_cycle = sum(_edit_counts.get(container_id, {}).values())
        # Fire if: edits exist AND (never fired OR new edits since last fire)
        if edit_cycle > 0 and (not state["fired"] or state["edit_cycle"] < edit_cycle):
            if _has_uncommitted_edits(self, root):
                review_output = _run_review_patch(self, container_id)
                new_state = {
                    "fired": True,
                    "edit_cycle": edit_cycle,
                    "findings_count": 0,
                    "high_confidence_count": 0,
                    "duplicate_suppressed": 0,
                    "submit_paused": False,
                }
                if review_output:
                    # Count findings in the output
                    finding_lines = [l for l in review_output.split("\n")
                                     if l.strip().startswith("[")]
                    new_state["findings_count"] = len(finding_lines)
                    new_state["high_confidence_count"] = sum(
                        1 for l in finding_lines if l.strip().startswith("[VERIFIED]"))
                    new_state["submit_paused"] = True
                    result["output"] = result.get("output", "") + "\n\n" + review_output
                    logger.info(
                        "vNext review_patch pre-submit for %s: %d findings (%d high-confidence)",
                        container_id, new_state["findings_count"],
                        new_state["high_confidence_count"],
                    )
                _review_state[container_id] = new_state
        # For git review commands, return here (they are readonly)
        if _is_git_review_command(command):
            return result

    # Skip other read-only commands
    readonly = {"grep", "cat", "find", "ls", "head", "tail", "wc", "diff", "git",
                "python3", "python", "echo", "cd", "pwd", "which", "pip", "pip3",
                "apt", "apt-get", "conda", "test", "file", "stat", "du", "df"}
    if first_word in readonly and ">" not in command and ">>" not in command:
        return result

    # ── event_brief: after non-readonly commands, check for modified files ──
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
                ext = os.path.splitext(fpath)[1]
                if ext in {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
                           ".rb", ".php", ".c", ".cpp", ".h", ".cs", ".cjs", ".mjs"}:
                    if _is_repo_source(fpath):
                        gt_output = _run_gt_intel(self, fpath)
                        if gt_output:
                            result["output"] = result.get("output", "") + gt_output
                            break
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

        # vNext review_patch: log pre-submit state from _hooked_execute.
        # Post-run logging here is telemetry only — the live review_patch
        # fires inside _hooked_execute where the agent can respond.
        if env is not None:
            cid = getattr(env, "container_id", "")
            rs = _review_state.get(cid, {})
            extra_info["review_patch_called_pre_submit"] = rs.get("fired", False)
            extra_info["submit_paused_for_review"] = rs.get("submit_paused", False)
            extra_info["review_findings_count"] = rs.get("findings_count", 0)
            extra_info["review_high_confidence_count"] = rs.get("high_confidence_count", 0)

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
            _novelty_seen.pop(cid, None)
            _review_state.pop(cid, None)

        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "vnext_decision_interface",
                        "gt_delivery": "task_map_event_brief_review_patch",
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
