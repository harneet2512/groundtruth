#!/usr/bin/env python3
"""OpenHands wrapper for gt_hook.py — amalgamated passive post-edit hook.

Injects gt_hook.py (self-contained, stdlib-only) into SWE-bench containers
and registers it as a PostToolUse hook on file_editor operations.  After each
task the container log (/tmp/gt_hook_log.jsonl) is extracted and saved next
to the trajectory for offline analysis.

Usage:
    python oh_gt_hook_wrapper.py .llm_config/vertex_qwen3.json \\
        --workspace docker \\
        --max-iterations 100 \\
        --num-workers 5 \\
        [extra args passed through to OpenHands main()]
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR   = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_HOOK_TOOL  = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_hook.py")


def patch_and_run() -> None:
    # ------------------------------------------------------------------ load
    if not os.path.exists(_HOOK_TOOL):
        print(f"ERROR: gt_hook.py not found at {_HOOK_TOOL}")
        sys.exit(1)

    with open(_HOOK_TOOL, "rb") as fh:
        hook_bytes = fh.read()

    gt_b64   = base64.b64encode(hook_bytes).decode("ascii")
    CHUNK    = 8000
    chunks   = [gt_b64[i: i + CHUNK] for i in range(0, len(gt_b64), CHUNK)]

    print(f"gt_hook.py: {len(hook_bytes):,} bytes  |  {len(gt_b64):,} b64  |  {len(chunks)} chunks")

    # ------------------------------------------------------------------ patch
    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main  # type: ignore[import]
    from openhands.sdk.hooks.config import HookConfig, HookDefinition, HookMatcher  # type: ignore[import]

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):  # type: ignore[override]
        instance_id = getattr(instance, "instance_id", str(instance))

        # Step 1 — inject gt_hook.py via base64 chunks
        ok = True
        for i, chunk in enumerate(chunks):
            op  = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_hook.b64")
            if res.exit_code != 0:
                print(f"  WARNING: chunk {i}/{len(chunks)} write failed for {instance_id}")
                ok = False
                break

        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && "
                "chmod +x /tmp/gt_hook.py && "
                "rm -f /tmp/gt_hook.b64 && "
                "echo GT_HOOK_READY"
            )
            if "GT_HOOK_READY" in (res.stdout or ""):
                print(f"  gt_hook injected: {instance_id}")
            else:
                print(f"  WARNING: gt_hook injection uncertain for {instance_id}")
        else:
            print(f"  WARNING: gt_hook injection FAILED — running without hook: {instance_id}")
            return _original_evaluate(self, instance, workspace)

        # Step 2 — run the task (hook injected via Conversation.__new__ patch below)
        result = _original_evaluate(self, instance, workspace)

        # Step 4 — extract hook log from container
        _extract_hook_log(workspace, instance_id)

        return result

    SWEBenchEvaluation.evaluate_instance = patched_evaluate

    # Also patch Conversation factory so the hook fires in every conversation
    try:
        from openhands.sdk.conversation import Conversation  # type: ignore[import]
        _orig_new = Conversation.__new__

        def patched_new(cls, *args, **kwargs):  # type: ignore[override]
            if not kwargs.get("hook_config"):
                kwargs["hook_config"] = HookConfig(
                    post_tool_use=[
                        HookMatcher(
                            matcher="file_editor",
                            hooks=[HookDefinition(
                                command=(
                                    "python3 /tmp/gt_hook.py "
                                    "--root=/testbed --db=/tmp/gt_index.db "
                                    "--quiet --max-items=3 2>/dev/null || true"
                                ),
                                timeout=20,
                            )],
                        )
                    ]
                )
            return _orig_new(cls, *args, **kwargs)

        Conversation.__new__ = patched_new
        print("Patched Conversation.__new__ with GT hook")
    except Exception as exc:
        print(f"  WARNING: Could not patch Conversation: {exc}")

    print(f"Patched SWEBenchEvaluation with gt_hook.py (passive evidence)")
    print()
    main()


def _extract_hook_log(workspace, instance_id: str) -> None:
    """Copy /tmp/gt_hook_log.jsonl out of the container to the results dir."""
    try:
        res = workspace.execute_command("cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''")
        if not res.stdout or not res.stdout.strip():
            return
        # Save next to other results — output dir is managed by OpenHands
        out_dir = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(out_dir, f"{instance_id}.jsonl")
        with open(log_path, "w") as fh:
            fh.write(res.stdout)
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
