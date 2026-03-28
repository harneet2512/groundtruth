#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench with GT v8 precomputed context.

Zero-tax approach: Run gt_hook.py understand on key files BEFORE the agent
starts, inject cross-file analysis into the instance prompt. Agent gets
callers, test files, and norms for FREE — zero iteration cost.

Usage:
    source ~/gt-venv/bin/activate && source ~/gt-env.sh
    python run_mini_gt_v8_precompute.py \
        -c mini_swebench_gt_v7.yaml \
        --model openai/gpt-5.4-nano \
        --subset lite --split test --slice 0:50 -w 4 -o ~/results/v8
"""
from __future__ import annotations

import base64
import re
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

GT_HOOK_PATH = Path(__file__).parent / "gt_hook.py"
_GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii")

# Chunk the base64 for large files (gt_hook.py is ~115KB)
_CHUNK_SIZE = 50_000
_CHUNKS = [_GT_HOOK_B64[i:i + _CHUNK_SIZE] for i in range(0, len(_GT_HOOK_B64), _CHUNK_SIZE)]

logger.info("GT v8 precompute: %d bytes, %d chunks", GT_HOOK_PATH.stat().st_size, len(_CHUNKS))


def _exec(env, cmd: str, timeout: int = 60):
    return env.execute({"command": cmd}, timeout=timeout)


def _inject_hook(env, instance_id: str) -> bool:
    """Inject gt_hook.py into container via chunked base64."""
    try:
        for i, chunk in enumerate(_CHUNKS):
            op = ">" if i == 0 else ">>"
            _exec(env, f"echo -n '{chunk}' {op} /tmp/gt_hook.b64", timeout=15)
        _exec(env, "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64", timeout=15)
        logger.info("gt_hook.py injected: %s", instance_id)
        return True
    except Exception as e:
        logger.warning("gt_hook injection failed for %s: %s", instance_id, e)
        return False


def _extract_file_paths(problem_statement: str, repo_name: str) -> list[str]:
    """Extract likely file paths from issue description."""
    files = set()
    # P1: Stack traces: File "path", line N
    for line in problem_statement.split("\n"):
        m = re.search(r'File "([^"]+\.py)"', line)
        if m:
            path = m.group(1)
            if repo_name and repo_name in path:
                path = path.split(repo_name + "/")[-1]
            files.add(path)
    # P2: Bare .py paths with slashes
    for m in re.findall(r'[a-zA-Z][\w]*/[a-zA-Z][\w/]*\.py', problem_statement):
        if not m.startswith("http"):
            files.add(m)
    # P3: Module paths (django.contrib.auth.tokens → django/contrib/auth/tokens.py)
    for m in re.findall(r'[a-z][\w]*(?:\.[a-z][\w]*){2,}', problem_statement):
        if not m.startswith("http"):
            f = m.replace(".", "/") + ".py"
            files.add(f)
    # P4: Backtick-quoted filenames (`fitsrec.py`, `tokens.py`)
    for m in re.findall(r'`(\w+\.py)`', problem_statement):
        files.add(m)
    # P5: Backtick-quoted module paths (`astropy.io.fits.fitsrec`)
    for m in re.findall(r'`([a-z][\w]*(?:\.[a-z][\w]*)+)`', problem_statement):
        if not any(c.isdigit() for c in m.split(".")[0]):
            f = m.replace(".", "/") + ".py"
            files.add(f)
    # Filter out non-paths
    filtered = []
    for f in sorted(files):
        parts = f.replace(".py", "").split("/")
        if any(p.isdigit() or (p and p[0].isdigit()) for p in parts):
            continue
        if parts[0] in ("self", "os", "sys", "re"):
            continue
        filtered.append(f)
    return filtered[:5]


# Common English/docs words to skip in class name extraction
_SKIP_WORDS = frozenset({
    "TypeError", "ValueError", "AttributeError", "KeyError", "ImportError",
    "RuntimeError", "NotImplementedError", "IndexError", "StopIteration",
    "FileNotFoundError", "PermissionError", "AssertionError", "OverflowError",
    "Python", "Django", "GitHub", "Linux", "Windows", "MacOS", "Docker",
    "Description", "Example", "Expected", "Actual", "Reproduce", "Version",
    "Versions", "Possible", "Incorrect", "Seeing", "Second", "Currently",
    "However", "Instead", "Perhaps", "Commenting", "Replace", "Because",
    "Before", "After", "LocalFiles", "Uploads", "MyModel",
})


def _extract_class_names(problem_statement: str) -> list[str]:
    """Extract class names from issue description using multiple strategies."""
    names = set()
    # Strategy 1: Known framework suffixes (most reliable)
    suffixes = (
        "Field", "Widget", "Form", "View", "Model", "Admin", "Manager",
        "Serializer", "Validator", "Backend", "Middleware", "Handler",
        "Mixin", "Base", "Storage", "Compiler", "Ref", "Data", "Error",
    )
    suffix_pat = "|".join(suffixes)
    for m in re.findall(rf'\b(\w*(?:{suffix_pat}))\b', problem_statement):
        if m not in _SKIP_WORDS and len(m) > 4 and m[0].isupper():
            names.add(m)
    # Strategy 2: CamelCase (relaxed — any uppercase word 5+ chars with mixed case)
    for m in re.findall(r'\b([A-Z][A-Za-z]{4,})\b', problem_statement):
        if m not in _SKIP_WORDS and any(c.islower() for c in m) and any(c.isupper() for c in m[1:]):
            names.add(m)
    # Strategy 3: Framework dotted patterns (models.FilePathField → FilePathField)
    for m in re.findall(r'\b(?:models|forms|admin|views|widgets|fields|auth|contrib|db|http|utils)\\.(\w+)', problem_statement):
        if m[0].isupper() and len(m) > 3:
            names.add(m)
    return sorted(names, key=len, reverse=True)[:8]


def _precompute_context(env, instance_id: str, problem_statement: str) -> str:
    """Run understand on key files and return formatted context."""
    repo_name = instance_id.split("__")[0].replace("-", "/").split("/")[-1] if "__" in instance_id else ""

    likely_files = _extract_file_paths(problem_statement, repo_name)
    context_parts = []

    for fpath in likely_files[:3]:
        try:
            # If bare filename (no slashes), find it in repo first
            if "/" not in fpath:
                find_result = _exec(env, f"find /testbed -name '{fpath}' -not -path '*/test*' -not -path '*__pycache__*' | head -1", timeout=10)
                found = (find_result.get("output", "") if isinstance(find_result, dict) else str(find_result)).strip()
                full_path = found if found else f"/testbed/{fpath}"
            else:
                full_path = f"/testbed/{fpath}" if not fpath.startswith("/") else fpath
            result = _exec(
                env,
                f"python3 /tmp/gt_hook.py understand {full_path} --root=/testbed --quiet --max-lines=10",
                timeout=60,
            )
            # env.execute returns dict with "output" key or sometimes the output directly
            if isinstance(result, dict):
                output = result.get("output", "").strip()
            elif isinstance(result, str):
                output = result.strip()
            else:
                output = str(result).strip()
            logger.info("  understand %s: type=%s len=%d first80=%s", fpath, type(result).__name__, len(output), repr(output[:80]))
            if output and len(output) > 20 and "Error" not in output[:50] and "Traceback" not in output[:50]:
                context_parts.append(f"## {fpath}\n{output}")
                logger.info("  precomputed: %s (%d chars)", fpath, len(output))
        except Exception as e:
            logger.warning("  understand %s failed: %s", fpath, e)

    if not context_parts:
        # Fallback: grep for class names and function names from the issue
        logger.info("  No file paths found, trying grep fallback for %s", instance_id)
        class_names = _extract_class_names(problem_statement)
        logger.info("  Class names extracted: %s", class_names[:5])
        # Also extract snake_case function names
        func_names = re.findall(r'\b([a-z][a-z_]+(?:_[a-z]+)+)\b', problem_statement)
        # Dedupe and pick most likely (longer names are more specific)
        search_terms = []
        for cn in class_names:
            search_terms.append(("class", cn))
        for fn in sorted(set(func_names), key=len, reverse=True)[:3]:
            if len(fn) > 5:  # skip short generic names
                search_terms.append(("def", fn))

        files_tried = set()
        for kind, name in search_terms[:5]:
            if len(context_parts) >= 2:
                break
            try:
                grep_cmd = f"grep -rn '{kind} {name}' /testbed --include='*.py' -l | grep -v test | grep -v __pycache__ | head -3"
                logger.info("  grep: %s %s", kind, name)
                result = _exec(env, grep_cmd, timeout=10)
                output = result.get("output", "").strip() if isinstance(result, dict) else ""
                logger.info("  grep result: %s", repr(output[:100]))
                for sf in output.split("\n")[:1]:
                    sf = sf.strip()
                    if not sf or sf in files_tried:
                        continue
                    files_tried.add(sf)
                    r = _exec(
                        env,
                        f"python3 /tmp/gt_hook.py understand {sf} --root=/testbed --quiet --max-lines=10",
                        timeout=60,
                    )
                    out = r.get("output", "").strip() if isinstance(r, dict) else str(r).strip()
                    if out and len(out) > 20 and "Error" not in out[:50] and "Traceback" not in out[:50]:
                        rel = sf.replace("/testbed/", "")
                        context_parts.append(f"## {rel}\n{out}")
                        logger.info("  precomputed (grep %s %s): %s (%d chars)", kind, name, rel, len(out))
            except Exception:
                pass

    if context_parts:
        return (
            "\n<gt_codebase_context>\n"
            "Pre-computed cross-file analysis for key files. Use this to inform your fix "
            "— especially callers and test locations. You do NOT need to run additional analysis.\n\n"
            + "\n\n".join(context_parts)
            + "\n</gt_codebase_context>\n"
        )
    return ""


def v8_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Process instance with GT v8 precomputed context injection."""
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
    gt_context = ""

    try:
        env = get_sb_environment(config, instance)

        # Step 1: Inject gt_hook.py
        progress_manager.update_instance_status(instance_id, "GT v8: injecting hook")
        hook_ok = _inject_hook(env, instance_id)
        extra_info["hook_injected"] = hook_ok

        # Step 2: Precompute context (zero cost to agent)
        if hook_ok:
            progress_manager.update_instance_status(instance_id, "GT v8: precomputing context")
            gt_context = _precompute_context(env, instance_id, original_task)
            extra_info["gt_context_chars"] = len(gt_context)
            if gt_context:
                logger.info("GT context: %d chars for %s", len(gt_context), instance_id)

        # Step 3: Build task with GT context prepended
        task = gt_context + "\n" + original_task if gt_context else original_task

        # Step 4: Run agent
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

        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "v8_precompute",
                        "gt_delivery": "precomputed_context",
                        "gt_context_chars": len(gt_context),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )

        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch
swebench_module.process_instance = v8_process_instance

if __name__ == "__main__":
    app()
