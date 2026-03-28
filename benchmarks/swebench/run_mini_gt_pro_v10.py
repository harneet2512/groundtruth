#!/usr/bin/env python3
"""Run mini-SWE-agent on SWE-bench Pro with GT v10 ego-graph precompute.

Same logic as run_mini_gt_v8_precompute.py but:
  - Uses /app as root (SWE-bench Pro containers use /app, not /testbed)
  - Loads from ScaleAI/SWE-bench_Pro dataset
  - Docker images: jefzda/sweap-images:{dockerhub_tag}

Usage:
    source ~/gt-venv/bin/activate && source ~/gt-env.sh
    python run_mini_gt_pro_v10.py swebench \
        -c benchmarks/swebench/mini_swebench_pro_gt_v10.yaml \
        --model openai/qwen3-coder \
        --subset pro --split test --slice 0:5 -w 2 -o ~/results/v10_pro
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

# --- Configuration: /app for SWE-bench Pro ---
REPO_ROOT = "/app"

GT_HOOK_PATH = Path(__file__).parent / "gt_hook.py"
_GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii")

_CHUNK_SIZE = 50_000
_CHUNKS = [_GT_HOOK_B64[i:i + _CHUNK_SIZE] for i in range(0, len(_GT_HOOK_B64), _CHUNK_SIZE)]

logger.info("GT v10 Pro precompute: %d bytes, %d chunks, root=%s",
            GT_HOOK_PATH.stat().st_size, len(_CHUNKS), REPO_ROOT)


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
    for line in problem_statement.split("\n"):
        m = re.search(r'File "([^"]+\.py)"', line)
        if m:
            path = m.group(1)
            if repo_name and repo_name in path:
                path = path.split(repo_name + "/")[-1]
            files.add(path)
    for m in re.findall(r'[a-zA-Z][\w]*/[a-zA-Z][\w/]*\.py', problem_statement):
        if not m.startswith("http"):
            files.add(m)
    for m in re.findall(r'[a-z][\w]*(?:\.[a-z][\w]*){2,}', problem_statement):
        if not m.startswith("http"):
            f = m.replace(".", "/") + ".py"
            files.add(f)
    for m in re.findall(r'`(\w+\.py)`', problem_statement):
        files.add(m)
    for m in re.findall(r'`([a-z][\w]*(?:\.[a-z][\w]*)+)`', problem_statement):
        if not any(c.isdigit() for c in m.split(".")[0]):
            f = m.replace(".", "/") + ".py"
            files.add(f)
    filtered = []
    for f in sorted(files):
        parts = f.replace(".py", "").split("/")
        if any(p.isdigit() or (p and p[0].isdigit()) for p in parts):
            continue
        if parts[0] in ("self", "os", "sys", "re"):
            continue
        filtered.append(f)
    return filtered[:5]


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
    suffixes = (
        "Field", "Widget", "Form", "View", "Model", "Admin", "Manager",
        "Serializer", "Validator", "Backend", "Middleware", "Handler",
        "Mixin", "Base", "Storage", "Compiler", "Ref", "Data", "Error",
    )
    suffix_pat = "|".join(suffixes)
    for m in re.findall(rf'\b(\w*(?:{suffix_pat}))\b', problem_statement):
        if m not in _SKIP_WORDS and len(m) > 4 and m[0].isupper():
            names.add(m)
    for m in re.findall(r'\b([A-Z][A-Za-z]{4,})\b', problem_statement):
        if m not in _SKIP_WORDS and any(c.islower() for c in m) and any(c.isupper() for c in m[1:]):
            names.add(m)
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
            if "/" not in fpath:
                find_result = _exec(env, f"find {REPO_ROOT} -name '{fpath}' -not -path '*/test*' -not -path '*__pycache__*' | head -1", timeout=10)
                found = (find_result.get("output", "") if isinstance(find_result, dict) else str(find_result)).strip()
                full_path = found if found else f"{REPO_ROOT}/{fpath}"
            else:
                full_path = f"{REPO_ROOT}/{fpath}" if not fpath.startswith("/") else fpath
            result = _exec(
                env,
                f"python3 /tmp/gt_hook.py understand {full_path} --root={REPO_ROOT} --quiet --max-lines=10",
                timeout=60,
            )
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
        logger.info("  No file paths found, trying grep fallback for %s", instance_id)
        class_names = _extract_class_names(problem_statement)
        logger.info("  Class names extracted: %s", class_names[:5])
        func_names = re.findall(r'\b([a-z][a-z_]+(?:_[a-z]+)+)\b', problem_statement)
        search_terms = []
        for cn in class_names:
            search_terms.append(("class", cn))
        for fn in sorted(set(func_names), key=len, reverse=True)[:3]:
            if len(fn) > 5:
                search_terms.append(("def", fn))

        files_tried = set()
        for kind, name in search_terms[:5]:
            if len(context_parts) >= 2:
                break
            try:
                grep_cmd = f"grep -rn '{kind} {name}' {REPO_ROOT} --include='*.py' -l | grep -v '/tests/' | grep -v __pycache__ | head -3"
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
                        f"python3 /tmp/gt_hook.py understand {sf} --root={REPO_ROOT} --quiet --max-lines=10",
                        timeout=60,
                    )
                    out = r.get("output", "").strip() if isinstance(r, dict) else str(r).strip()
                    if out and len(out) > 20 and "Error" not in out[:50] and "Traceback" not in out[:50]:
                        rel = sf.replace(f"{REPO_ROOT}/", "")
                        context_parts.append(f"## {rel}\n{out}")
                        logger.info("  precomputed (grep %s %s): %s (%d chars)", kind, name, rel, len(out))
            except Exception:
                pass

    if context_parts:
        return (
            "\n=== CODEBASE INTELLIGENCE (pre-computed, read before editing) ===\n\n"
            + "\n\n".join(context_parts)
            + "\n\n=== END CODEBASE INTELLIGENCE ===\n"
        )
    return ""


def v10_pro_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Process SWE-bench Pro instance with GT v10 precomputed context."""
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
        progress_manager.update_instance_status(instance_id, "GT v10 Pro: injecting hook")
        hook_ok = _inject_hook(env, instance_id)
        extra_info["hook_injected"] = hook_ok

        # Step 2: Precompute context (zero cost to agent)
        if hook_ok:
            progress_manager.update_instance_status(instance_id, "GT v10 Pro: precomputing context")
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
                        "gt_version": "v10_ego_graph_pro",
                        "gt_delivery": "precomputed_context",
                        "gt_context_chars": len(gt_context),
                        "repo_root": REPO_ROOT,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )

        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


# Monkey-patch
swebench_module.process_instance = v10_pro_process_instance

if __name__ == "__main__":
    app()
