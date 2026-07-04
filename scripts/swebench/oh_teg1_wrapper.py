#!/usr/bin/env python3
"""OpenHands wrapper for the TEG-1 commit-prompt hook.

Companion to oh_gt_hook_wrapper.py but for a DIFFERENT hook:
this one fires once per task, BEFORE the agent has edited any gold-set
candidate file, to compress the touch->edit gap (see
touch_edit_gap_next_track.md).

What this wrapper does per task:
  1. Inject `gt_commit_prompt.py` to /tmp/ in the container (base64-chunked).
  2. Tell the V1R-map brief generator to also write its ranked candidate
     paths to /tmp/teg1_gold_paths.txt — done by setting the env var
     GT_TEG1_GOLD_PATHS_OUT, which the patched run_infer.py forwards as
     `--gold-paths-out=...` to the bundle.
  3. Configure /workspace/.openhands/hooks.json with a broad post_tool_use
     matcher that runs the commit-prompt hook after every tool call. The
     hook itself is one-shot (fires at most once per task).
  4. After the task, extract /tmp/teg1_log.jsonl and /tmp/teg1_state.json
     to GT_LOG_DIR/<instance_id>_teg1.{jsonl,state.json} for offline analysis.

This wrapper does NOT touch the V1R-map brief generation logic itself.
The brief stays at exactly its frozen behavior — only its sidecar gold-paths
file is added.

Compatible with:
  - The V1R-map arm of run_arm_v1r.py (no GT_HOOK_PATH set).
NOT compatible with:
  - The V1R-map+hook arm — that uses oh_gt_hook_wrapper.py for a different
    (post-edit, multi-fire) hook. Don't stack them; their interaction is
    explicitly out of scope per touch_edit_gap_next_track.md §9.

Usage on VM:
    GT_TEG1_GOLD_PATHS_OUT=/tmp/teg1_gold_paths.txt \
    python3 scripts/swebench/oh_teg1_wrapper.py \
        .llm_config/qwen3_or.json \
        --workspace docker --max-iterations 100 --num-workers 1 \
        [extra args passed through to OpenHands main()]
"""

from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_HOOK_TOOL = os.path.join(_SCRIPT_DIR, "gt_commit_prompt.py")

GOLD_PATHS_FILE = "/tmp/teg1_gold_paths.txt"
STATE_FILE = "/tmp/teg1_state.json"
LOG_FILE = "/tmp/teg1_log.jsonl"
HOOK_PATH_IN_CONTAINER = "/tmp/gt_commit_prompt.py"

_HOOKS_JSON = json.dumps({
    "post_tool_use": [
        {
            "matcher": ".*",
            "hooks": [
                {
                    "command": (
                        f"python3 {HOOK_PATH_IN_CONTAINER} "
                        f"--root=/workspace "
                        f"--gold-paths={GOLD_PATHS_FILE} "
                        f"--state={STATE_FILE} "
                        f"--log={LOG_FILE} "
                        f"--threshold=4 2>/dev/null || true"
                    ),
                    "timeout": 15,
                }
            ],
        }
    ]
})


def patch_and_run() -> None:
    if not os.path.exists(_HOOK_TOOL):
        print(f"ERROR: gt_commit_prompt.py not found at {_HOOK_TOOL}")
        sys.exit(1)

    with open(_HOOK_TOOL, "rb") as fh:
        hook_bytes = fh.read()

    gt_b64 = base64.b64encode(hook_bytes).decode("ascii")
    hooks_b64 = base64.b64encode(_HOOKS_JSON.encode("utf-8")).decode("ascii")
    CHUNK = 8000
    chunks = [gt_b64[i: i + CHUNK] for i in range(0, len(gt_b64), CHUNK)]

    print(
        f"gt_commit_prompt.py: {len(hook_bytes):,} bytes  "
        f"|  {len(gt_b64):,} b64  |  {len(chunks)} chunks",
        flush=True,
    )

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main  # type: ignore[import]

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):  # type: ignore[override]
        instance_id = getattr(instance, "instance_id", str(instance))
        print(f">>> TEG-1 PATCHED_EVALUATE: {instance_id} <<<", flush=True)

        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_commit_prompt.b64")
            if res.exit_code != 0:
                print(f"  WARNING: chunk {i}/{len(chunks)} write failed for {instance_id}")
                ok = False
                break
        if ok:
            res = workspace.execute_command(
                f"base64 -d /tmp/gt_commit_prompt.b64 > {HOOK_PATH_IN_CONTAINER} && "
                f"chmod +x {HOOK_PATH_IN_CONTAINER} && "
                "rm -f /tmp/gt_commit_prompt.b64 && "
                "echo TEG1_HOOK_READY"
            )
            if "TEG1_HOOK_READY" in (res.stdout or ""):
                print(f"  TEG-1 hook injected: {instance_id}")
            else:
                print(f"  WARNING: TEG-1 hook injection uncertain: {instance_id}")
        else:
            print(f"  WARNING: TEG-1 hook injection FAILED: {instance_id}")
            return _original_evaluate(self, instance, workspace)

        res = workspace.execute_command(
            "mkdir -p /workspace/.openhands && "
            f"echo '{hooks_b64}' | base64 -d > /workspace/.openhands/hooks.json && "
            "echo TEG1_HOOKS_JSON_READY"
        )
        if "TEG1_HOOKS_JSON_READY" in (res.stdout or ""):
            print(f"  TEG-1 hooks.json written: {instance_id}")
        else:
            print(f"  WARNING: TEG-1 hooks.json write uncertain: {instance_id}")

        workspace.execute_command(
            f"rm -f {STATE_FILE} {LOG_FILE} {GOLD_PATHS_FILE} && echo TEG1_STATE_RESET"
        )

        try:
            result = _original_evaluate(self, instance, workspace)
        finally:
            _extract_teg1_logs(workspace, instance_id)

        return result

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print(
        f"Patched SWEBenchEvaluation with TEG-1 commit-prompt hook  "
        f"(verify: {SWEBenchEvaluation.evaluate_instance is patched_evaluate})",
        flush=True,
    )

    os.environ.setdefault("GT_TEG1_GOLD_PATHS_OUT", GOLD_PATHS_FILE)
    main()


def _extract_teg1_logs(workspace, instance_id: str) -> None:
    """Pull /tmp/teg1_{log.jsonl,state.json,gold_paths.txt} out of the container."""
    out_dir = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")
    os.makedirs(out_dir, exist_ok=True)
    try:
        res = workspace.execute_command(f"cat {LOG_FILE} 2>/dev/null || echo ''")
        if res.stdout and res.stdout.strip():
            log_path = os.path.join(out_dir, f"{instance_id}_teg1.jsonl")
            with open(log_path, "w") as fh:
                fh.write(res.stdout)
            print(f"  TEG-1 log extracted: {instance_id} ({len(res.stdout)} bytes)")

        res2 = workspace.execute_command(f"cat {STATE_FILE} 2>/dev/null || echo ''")
        if res2.stdout and res2.stdout.strip():
            state_path = os.path.join(out_dir, f"{instance_id}_teg1.state.json")
            with open(state_path, "w") as fh:
                fh.write(res2.stdout)

        res3 = workspace.execute_command(f"cat {GOLD_PATHS_FILE} 2>/dev/null || echo ''")
        if res3.stdout and res3.stdout.strip():
            gp_path = os.path.join(out_dir, f"{instance_id}_teg1.gold_paths.txt")
            with open(gp_path, "w") as fh:
                fh.write(res3.stdout)
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
