#!/usr/bin/env python3
"""OpenHands wrapper for gt_hook.py — amalgamated passive post-edit hook.

Injects gt_hook.py into SWE-bench containers and writes .openhands/hooks.json
so the OpenHands HookManager automatically loads the PostToolUse hook config.
After each task, extracts /tmp/gt_hook_log.jsonl for offline analysis.

Usage:
    python oh_gt_hook_wrapper.py .llm_config/vertex_qwen3.json \\
        --workspace docker \\
        --max-iterations 100 \\
        --num-workers 5 \\
        [extra args passed through to OpenHands main()]
"""

from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR   = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_HOOK_TOOL  = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_hook.py")

# The hooks.json content that OpenHands HookManager will load automatically
# from .openhands/hooks.json inside the workspace
_HOOKS_JSON = json.dumps({
    "post_tool_use": [
        {
            "matcher": "file_editor",
            "hooks": [
                {
                    "command": (
                        "python3 /tmp/gt_hook.py "
                        "--root=/workspace --db=/tmp/gt_index.db "
                        "--quiet --max-items=3 2>/dev/null || true"
                    ),
                    "timeout": 20,
                }
            ]
        }
    ]
})


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

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):  # type: ignore[override]
        print(f">>> PATCHED_EVALUATE CALLED <<<", flush=True)
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
                print(f"  gt_hook.py injected: {instance_id}")
            else:
                print(f"  WARNING: gt_hook injection uncertain: {instance_id}")
        else:
            print(f"  WARNING: gt_hook injection FAILED: {instance_id}")
            return _original_evaluate(self, instance, workspace)

        # Step 2 — write .openhands/hooks.json so HookManager loads it
        hooks_cmd = (
            "mkdir -p /workspace/.openhands && "
            f"echo '{_HOOKS_JSON}' > /workspace/.openhands/hooks.json && "
            "echo HOOKS_JSON_READY"
        )
        res = workspace.execute_command(hooks_cmd)
        if "HOOKS_JSON_READY" in (res.stdout or ""):
            print(f"  hooks.json written: {instance_id}")
        else:
            print(f"  WARNING: hooks.json write uncertain: {instance_id}")

        # Also write at repo root in case workspace is there
        workspace.execute_command(
            "mkdir -p /testbed/.openhands && "
            f"echo '{_HOOKS_JSON}' > /testbed/.openhands/hooks.json"
        )

        # Step 3 — run the task
        result = _original_evaluate(self, instance, workspace)

        # Step 4 — extract hook log from container
        _extract_hook_log(workspace, instance_id)

        return result

    SWEBenchEvaluation.evaluate_instance = patched_evaluate

    # Also try patching Conversation to inject hook_config via API
    try:
        from openhands.sdk.hooks.config import HookConfig, HookDefinition, HookMatcher  # type: ignore[import]
        from openhands.sdk.conversation.impl.remote_conversation import RemoteConversation  # type: ignore[import]

        GT_HOOK_CONFIG = HookConfig(
            post_tool_use=[
                HookMatcher(
                    matcher="file_editor",
                    hooks=[HookDefinition(
                        command=(
                            "python3 /tmp/gt_hook.py "
                            "--root=/workspace --db=/tmp/gt_index.db "
                            "--quiet --max-items=3 2>/dev/null || true"
                        ),
                        timeout=20,
                    )],
                )
            ]
        )

        _orig_remote_init = RemoteConversation.__init__

        def patched_remote_init(self_conv, *args, **kwargs):  # type: ignore[override]
            # Force hook_config into RemoteConversation before payload is built
            if "hook_config" not in kwargs or kwargs.get("hook_config") is None:
                kwargs["hook_config"] = GT_HOOK_CONFIG
            try:
                with open("/tmp/gt_remote_init.txt", "a") as _f:
                    _f.write(f"RemoteConversation.__init__ hook_config={'SET' if kwargs.get('hook_config') else 'NONE'}\n")
            except Exception:
                pass
            return _orig_remote_init(self_conv, *args, **kwargs)

        RemoteConversation.__init__ = patched_remote_init
        print("Patched RemoteConversation.__init__ with GT hook_config")
    except Exception as exc:
        print(f"  WARNING: Could not patch Conversation: {exc}")

    # Verify the patch sticks
    print(f"Patched SWEBenchEvaluation with gt_hook.py (passive evidence + hooks.json)")
    print(f"  verify: evaluate_instance is patched = {SWEBenchEvaluation.evaluate_instance is patched_evaluate}")
    print(f"  verify: in __dict__ = {'evaluate_instance' in SWEBenchEvaluation.__dict__}")
    print(flush=True)
    main()


def _extract_hook_log(workspace, instance_id: str) -> None:
    """Copy /tmp/gt_hook_log.jsonl out of the container to the results dir."""
    try:
        res = workspace.execute_command("cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''")
        if not res.stdout or not res.stdout.strip():
            return
        out_dir = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(out_dir, f"{instance_id}.jsonl")
        with open(log_path, "w") as fh:
            fh.write(res.stdout)
        print(f"  hook log extracted: {instance_id} ({len(res.stdout)} bytes)")
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
