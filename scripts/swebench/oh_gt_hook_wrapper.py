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

        # Step 2 — start Python polling watcher that runs gt_hook.py on .py changes
        watcher_script = r'''
import os, time, subprocess, json
WATCH_DIR = "/workspace"
HOOK_CMD = ["python3", "/tmp/gt_hook.py", "--root=/workspace", "--db=/tmp/gt_index.db", "--quiet", "--max-items=3"]
POLL_INTERVAL = 2
STDOUT_LOG = "/tmp/gt_hook_stdout.log"

# Build initial snapshot
def get_mtimes(d):
    mtimes = {}
    for root, dirs, files in os.walk(d):
        if "/.git/" in root or "/__pycache__/" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                fp = os.path.join(root, f)
                try:
                    mtimes[fp] = os.path.getmtime(fp)
                except OSError:
                    pass
    return mtimes

prev = get_mtimes(WATCH_DIR)
with open(STDOUT_LOG, "a") as log:
    log.write(f"watcher started: {len(prev)} py files\n")
    log.flush()
    while True:
        time.sleep(POLL_INTERVAL)
        curr = get_mtimes(WATCH_DIR)
        changed = [f for f in curr if f not in prev or curr[f] != prev.get(f)]
        new_files = [f for f in curr if f not in prev]
        if changed or new_files:
            log.write(f"change detected: {len(changed)} modified, {len(new_files)} new\n")
            log.flush()
            try:
                r = subprocess.run(HOOK_CMD, capture_output=True, text=True, timeout=20)
                if r.stdout.strip():
                    log.write(r.stdout)
                    log.flush()
            except Exception as e:
                log.write(f"hook error: {e}\n")
                log.flush()
            prev = curr
        else:
            prev = curr
'''
        # Write watcher script to container
        import base64 as _b64
        watcher_b64 = _b64.b64encode(watcher_script.encode()).decode()
        res = workspace.execute_command(
            f"echo '{watcher_b64}' | base64 -d > /tmp/gt_watcher.py && "
            "nohup python3 /tmp/gt_watcher.py > /dev/null 2>&1 & "
            "echo WATCHER_PID=$!"
        )
        if "WATCHER_PID=" in (res.stdout or ""):
            print(f"  py watcher started: {instance_id} ({res.stdout.strip()})")
        else:
            print(f"  WARNING: watcher failed: {instance_id}")

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
    """Copy /tmp/gt_hook_log.jsonl and stdout log out of the container."""
    out_dir = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")
    os.makedirs(out_dir, exist_ok=True)
    try:
        # JSONL structured log
        res = workspace.execute_command("cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''")
        if res.stdout and res.stdout.strip():
            log_path = os.path.join(out_dir, f"{instance_id}.jsonl")
            with open(log_path, "w") as fh:
                fh.write(res.stdout)
            print(f"  hook JSONL extracted: {instance_id} ({len(res.stdout)} bytes)")

        # Stdout log from inotify watcher
        res2 = workspace.execute_command("cat /tmp/gt_hook_stdout.log 2>/dev/null || echo ''")
        if res2.stdout and res2.stdout.strip():
            stdout_path = os.path.join(out_dir, f"{instance_id}_stdout.log")
            with open(stdout_path, "w") as fh:
                fh.write(res2.stdout)
            print(f"  hook stdout extracted: {instance_id} ({len(res2.stdout)} bytes)")
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
