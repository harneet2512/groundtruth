#!/usr/bin/env python3
"""Run mini-SWE-agent on DeepSWE with GroundTruth injection.

Adapts the proven run_mini_gt_v7.py pattern for DeepSWE's multi-language,
multi-repo benchmark. Brief is injected once before the first agent turn.
gt_hook.py is available in-container for on-demand understand/verify.

Usage:
    python patch_mini_swe.py swebench \
        -c deepswe_gt.yaml \
        --model deepseek/deepseek-v4-flash \
        --instance-id dateutil-rfc5545-timezone-interop \
        -o ~/results/deepswe_gt

Environment:
    GT_PREBUILT_INDEXES_ROOT  — directory with {instance_id}/graph.db
    GT_REPO_EXTRACTS_ROOT     — directory with cloned repos (optional)
    GT_INDEX_BINARY           — path to gt-index-linux (for in-container indexing fallback)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path

# Add GT source to path
_GT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _GT_ROOT.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

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

from inject import generate_deepswe_brief

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INDEXES_ROOT = os.environ.get("GT_PREBUILT_INDEXES_ROOT", "")
REPO_EXTRACTS_ROOT = os.environ.get("GT_REPO_EXTRACTS_ROOT", "")
GT_INDEX_BINARY = os.environ.get("GT_INDEX_BINARY", "")

# gt_hook.py path (reuse from benchmarks/swebench/)
GT_HOOK_PATH = Path(__file__).resolve().parents[1].parent / "benchmarks" / "swebench" / "gt_hook.py"
if not GT_HOOK_PATH.exists():
    GT_HOOK_PATH = Path(__file__).resolve().parent / "gt_hook.py"

# Pre-encode hook for injection
_GT_HOOK_B64 = ""
_GT_HOOK_CHUNKS: list[str] = []
_CHUNK_SIZE = 50_000

if GT_HOOK_PATH.exists():
    _GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii")
    _GT_HOOK_CHUNKS = [
        _GT_HOOK_B64[i : i + _CHUNK_SIZE]
        for i in range(0, len(_GT_HOOK_B64), _CHUNK_SIZE)
    ]
    logger.info(
        "GT hook: %d bytes, %d chunks from %s",
        GT_HOOK_PATH.stat().st_size,
        len(_GT_HOOK_CHUNKS),
        GT_HOOK_PATH,
    )
else:
    logger.warning("gt_hook.py not found at %s — hook injection disabled", GT_HOOK_PATH)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _exec(env, cmd: str, timeout: int = 60) -> dict:
    return env.execute({"command": cmd}, timeout=timeout)


def _detect_repo_root(env, instance_id: str) -> str:
    """Probe container for the repo root directory."""
    candidates = ["/testbed", "/home/user", "/workspace", "/app", "/repo"]
    for path in candidates:
        try:
            result = _exec(env, f"test -d {path}/.git && echo YES || echo NO", timeout=10)
            if "YES" in result.get("output", ""):
                logger.info("Repo root for %s: %s", instance_id, path)
                return path
        except Exception:
            continue

    # Fallback: find first .git directory
    try:
        result = _exec(env, "find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1", timeout=15)
        git_dir = result.get("output", "").strip()
        if git_dir:
            root = git_dir.rsplit("/.git", 1)[0]
            logger.info("Repo root for %s (via find): %s", instance_id, root)
            return root
    except Exception:
        pass

    logger.warning("Could not detect repo root for %s, defaulting to /testbed", instance_id)
    return "/testbed"


def _inject_hook(env, instance_id: str) -> bool:
    """Inject gt_hook.py into container via chunked base64."""
    if not _GT_HOOK_CHUNKS:
        return False

    try:
        for i, chunk in enumerate(_GT_HOOK_CHUNKS):
            op = ">" if i == 0 else ">>"
            _exec(env, f"echo -n '{chunk}' {op} /tmp/gt_hook.b64", timeout=15)
        _exec(
            env,
            "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64",
            timeout=15,
        )
        _exec(env, "chmod +x /tmp/gt_hook.py", timeout=5)

        # Verify
        result = _exec(env, "python3 /tmp/gt_hook.py understand --help 2>&1 | head -3", timeout=15)
        logger.info("GT hook injected for %s", instance_id)
        return True
    except Exception as e:
        logger.warning("GT hook injection failed for %s: %s", instance_id, e)
        return False


def _inject_index(env, instance_id: str, repo_root: str) -> bool:
    """Copy pre-built graph.db into container, or index in-container."""
    if INDEXES_ROOT:
        host_db = os.path.join(INDEXES_ROOT, instance_id, "graph.db")
        if os.path.exists(host_db):
            # Copy via base64 (graph.db is typically <5MB)
            try:
                with open(host_db, "rb") as f:
                    db_b64 = base64.b64encode(f.read()).decode("ascii")

                chunks = [db_b64[i:i+50_000] for i in range(0, len(db_b64), 50_000)]
                for i, chunk in enumerate(chunks):
                    op = ">" if i == 0 else ">>"
                    _exec(env, f"echo -n '{chunk}' {op} /tmp/graph.b64", timeout=15)
                _exec(
                    env,
                    "base64 -d /tmp/graph.b64 > /tmp/gt_index.db && rm -f /tmp/graph.b64",
                    timeout=15,
                )
                logger.info("Pre-built graph.db injected for %s", instance_id)
                return True
            except Exception as e:
                logger.warning("graph.db injection failed for %s: %s", instance_id, e)

    # Fallback: index in-container if binary is available
    if GT_INDEX_BINARY and os.path.exists(GT_INDEX_BINARY):
        try:
            with open(GT_INDEX_BINARY, "rb") as f:
                bin_b64 = base64.b64encode(f.read()).decode("ascii")

            chunks = [bin_b64[i:i+50_000] for i in range(0, len(bin_b64), 50_000)]
            for i, chunk in enumerate(chunks):
                op = ">" if i == 0 else ">>"
                _exec(env, f"echo -n '{chunk}' {op} /tmp/gt_index.b64", timeout=15)
            _exec(
                env,
                "base64 -d /tmp/gt_index.b64 > /tmp/gt-index && chmod +x /tmp/gt-index && rm -f /tmp/gt_index.b64",
                timeout=30,
            )
            _exec(
                env,
                f"/tmp/gt-index -root={repo_root} -output=/tmp/gt_index.db",
                timeout=300,
            )
            logger.info("In-container indexing completed for %s", instance_id)
            return True
        except Exception as e:
            logger.warning("In-container indexing failed for %s: %s", instance_id, e)

    return False


def _extract_hook_log(env, instance_id: str, output_dir: Path) -> Path | None:
    """Extract gt_hook log from container after run."""
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
    except Exception:
        pass
    return None


def _read_instruction(instance_id: str, tasks_dir: str = "") -> str:
    """Read instruction.md for a DeepSWE task."""
    if not tasks_dir:
        tasks_dir = str(Path(__file__).resolve().parents[1].parent / "deepswe-bench" / "tasks")

    instruction_path = os.path.join(tasks_dir, instance_id, "instruction.md")
    if os.path.exists(instruction_path):
        with open(instruction_path) as f:
            return f.read()
    return ""


# ---------------------------------------------------------------------------
# Main process_instance override
# ---------------------------------------------------------------------------

def deepswe_process_instance(config, instance, output_dir):
    """Process a single DeepSWE instance with GT injection."""
    instance_id = instance.get("instance_id", instance.get("task_id", "unknown"))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    gt_meta = {
        "instance_id": instance_id,
        "hook_injected": False,
        "index_injected": False,
        "brief_generated": False,
        "brief_length": 0,
        "repo_root": "",
    }

    try:
        env = get_sb_environment(config, instance)

        # 1. Detect repo root
        repo_root = _detect_repo_root(env, instance_id)
        gt_meta["repo_root"] = repo_root

        # 2. Inject gt_hook.py
        gt_meta["hook_injected"] = _inject_hook(env, instance_id)

        # 3. Inject or build graph.db
        gt_meta["index_injected"] = _inject_index(env, instance_id, repo_root)

        # 4. Update gt_hook.py to use the right paths
        if gt_meta["hook_injected"] and gt_meta["index_injected"]:
            try:
                _exec(env, f"export GT_DB=/tmp/gt_index.db GT_ROOT={repo_root}", timeout=5)
            except Exception:
                pass

        # 5. Generate brief on host
        problem_statement = instance.get("problem_statement", "")
        if not problem_statement:
            problem_statement = _read_instruction(instance_id)

        brief = ""
        if INDEXES_ROOT:
            graph_db = os.path.join(INDEXES_ROOT, instance_id, "graph.db")
            repo_extract = os.path.join(REPO_EXTRACTS_ROOT, instance_id) if REPO_EXTRACTS_ROOT else ""
            if os.path.exists(graph_db):
                brief = generate_deepswe_brief(
                    instance_id=instance_id,
                    problem_statement=problem_statement,
                    graph_db=graph_db,
                    repo_root=repo_extract or "/tmp",
                )

        gt_meta["brief_generated"] = bool(brief)
        gt_meta["brief_length"] = len(brief)

        # 6. Compose task with brief
        task = problem_statement
        if brief:
            task = f"{brief}\n\n{task}"

        # 7. Run the agent
        model = get_model(config, instance)
        agent = ProgressTrackingAgent(model, config["agent"])
        result = agent.run(task, env)

        # 8. Extract hook logs
        _extract_hook_log(env, instance_id, output_dir)

        # 9. Save predictions
        update_preds_file(output_dir, instance, result)

        # 10. Save GT metadata
        meta_path = output_dir / "gt_meta" / f"{instance_id}.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_path, "w") as f:
            json.dump(gt_meta, f, indent=2)

        logger.info(
            "Completed %s: brief=%d chars, hook=%s, index=%s",
            instance_id,
            gt_meta["brief_length"],
            gt_meta["hook_injected"],
            gt_meta["index_injected"],
        )

    except Exception as e:
        logger.error("Failed %s: %s\n%s", instance_id, e, traceback.format_exc())
        gt_meta["error"] = str(e)
        try:
            remove_from_preds_file(output_dir, instance)
        except Exception:
            pass

        meta_path = output_dir / "gt_meta" / f"{instance_id}.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_path, "w") as f:
            json.dump(gt_meta, f, indent=2)


# Override mini-swe-agent's process_instance
swebench_module.process_instance = deepswe_process_instance

if __name__ == "__main__":
    app()
