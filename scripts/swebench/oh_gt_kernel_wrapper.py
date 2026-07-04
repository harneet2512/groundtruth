#!/usr/bin/env python3
"""OpenHands wrapper for gt_kernel_check.py — runtime kernel decision hook.

Mirrors oh_gt_hook_wrapper.py shape: chunk-injects benchmarks/swebench/gt_kernel_check.py
into each SWE-bench-Live container as /tmp/gt_kernel_check.py, writes a
.openhands/hooks.json registering it under the file_editor PostToolUse matcher,
and reuses the same .py-mtime watcher pattern so the hook fires after each
agent edit.

Two arms supported via env var:
    GT_KERNEL_ARM=control   --> only chunk-injects v7.3 brief (existing flow)
    GT_KERNEL_ARM=kernel    --> brief + kernel post-edit hook on top

Usage (mimics oh_gt_hook_wrapper.py CLI exactly so it slots into existing
launcher scripts):
    python oh_gt_kernel_wrapper.py <llm_config> --workspace docker \\
        --max-iterations 100 --num-workers <N> [other OH args]
"""
from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_KERNEL_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_kernel_check.py")

ARM = os.environ.get("GT_KERNEL_ARM", "kernel").lower()
BRIEF_JSONL = os.environ.get("GT_KERNEL_BRIEF_JSONL", "/tmp/gt_pretask.jsonl")
EDIT_HISTORY = os.environ.get("GT_KERNEL_EDIT_HISTORY", "/tmp/gt_edits.jsonl")

# OH PostToolUse hook command — reads $OPENHANDS_FILE_PATH (the just-edited path)
# and falls back to discovering via git status if the env var is absent.
_HOOK_CMD = (
    "python3 /tmp/gt_kernel_check.py "
    "--edit-path \"${OPENHANDS_FILE_PATH:-$(cd /workspace && "
    "ls -d */ 2>/dev/null | head -1 | tr -d /)/$(cd /workspace && "
    "git -C $(ls -d */ 2>/dev/null | head -1) diff --name-only HEAD 2>/dev/null | head -1)}\" "
    f"--brief-jsonl {BRIEF_JSONL} "
    f"--edit-history {EDIT_HISTORY} "
    "2>/dev/null || true"
)

_HOOKS_JSON = json.dumps({
    "post_tool_use": [
        {
            "matcher": "file_editor",
            "hooks": [
                {"command": _HOOK_CMD, "timeout": 15},
            ],
        }
    ]
})


def install_kernel_patch() -> bool:
    """Verify the V4 in-source patch is present in run_infer.py and the hook
    file is on disk where the env var points. The actual chunk-injection +
    runtime.run_action wrap happens inside run_infer.py's GT_PATCH_V4 block,
    which is added once by scripts/swebench/patch_run_infer_for_kernel.py.

    This function used to monkey-patch SWEBenchEvaluation, but that class
    does not exist in the current OH evaluator surface — process_instance
    is the entry point and it reads GT_KERNEL_HOOK_PATH directly."""
    if ARM == "control":
        print("install_kernel_patch: ARM=control -- patch not needed")
        return True

    env_path = os.environ.get("GT_KERNEL_HOOK_PATH", "")
    if not env_path or not os.path.isfile(env_path):
        print(
            f"WARN: GT_KERNEL_HOOK_PATH={env_path!r} missing or not a file -- "
            "kernel hook will not fire in containers"
        )
        return False

    try:
        from evaluation.benchmarks.swe_bench import run_infer  # type: ignore[import]
    except ImportError:
        try:
            from benchmarks.swebench import run_infer  # type: ignore[import]
        except ImportError as exc:
            print(f"ERROR: cannot import run_infer: {exc}")
            return False

    src = open(run_infer.__file__, encoding="utf-8").read()
    if "GT_PATCH_V4" not in src:
        print(
            "WARN: GT_PATCH_V4 not present in run_infer.py. "
            "Run: python3 scripts/swebench/patch_run_infer_for_kernel.py"
        )
        return False
    print(
        f"install_kernel_patch OK  arm={ARM}  "
        f"hook={env_path}  V4 in {run_infer.__file__}"
    )
    return True


def _legacy_unused_block() -> None:
    """Old SWEBenchEvaluation patch logic — kept for reference but no longer
    invoked. Current OH evaluator surface uses process_instance + the V4
    in-source patch."""
    return
    with open(_KERNEL_TOOL, "rb") as fh:
        kernel_bytes = fh.read()

    k_b64 = base64.b64encode(kernel_bytes).decode("ascii")
    hooks_b64 = base64.b64encode(_HOOKS_JSON.encode("utf-8")).decode("ascii")
    CHUNK = 8000
    chunks = [k_b64[i:i + CHUNK] for i in range(0, len(k_b64), CHUNK)]

    print(
        f"install_kernel_patch  arm={ARM}  "
        f"gt_kernel_check.py: {len(kernel_bytes):,} bytes / "
        f"{len(k_b64):,} b64 / {len(chunks)} chunks"
    )

    try:
        from evaluation.benchmarks.swe_bench.run_infer import SWEBenchEvaluation  # type: ignore[import]
    except ImportError:
        from benchmarks.swebench.run_infer import SWEBenchEvaluation  # type: ignore[import]

    _orig_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):  # type: ignore[override]
        instance_id = getattr(instance, "instance_id", str(instance))

        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_kernel.b64")
            if res.exit_code != 0:
                print(f"  WARN chunk {i}/{len(chunks)} write failed: {instance_id}")
                ok = False
                break

        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_kernel.b64 > /tmp/gt_kernel_check.py && "
                "chmod +x /tmp/gt_kernel_check.py && "
                "rm -f /tmp/gt_kernel.b64 && "
                "rm -f " + EDIT_HISTORY + " && "
                "echo GT_KERNEL_READY"
            )
            if "GT_KERNEL_READY" in (res.stdout or ""):
                print(f"  gt_kernel_check.py injected: {instance_id}")
            else:
                print(f"  WARN injection uncertain: {instance_id}")
        else:
            print(f"  WARN injection FAILED: {instance_id}")
            return _orig_evaluate(self, instance, workspace)

        res = workspace.execute_command(
            "mkdir -p /workspace/.openhands && "
            f"echo '{hooks_b64}' | base64 -d > /workspace/.openhands/hooks.json && "
            "echo GT_KERNEL_HOOKS_READY"
        )
        if "GT_KERNEL_HOOKS_READY" in (res.stdout or ""):
            print(f"  hooks.json written: {instance_id}")
        else:
            print(f"  WARN hooks.json write uncertain: {instance_id}")

        try:
            return _orig_evaluate(self, instance, workspace)
        finally:
            _extract_kernel_log(workspace, instance_id)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate

    try:
        from openhands.sdk.hooks.config import HookConfig, HookDefinition, HookMatcher  # type: ignore[import]
        from openhands.sdk.conversation.impl.remote_conversation import RemoteConversation  # type: ignore[import]

        GT_KERNEL_HOOK_CONFIG = HookConfig(
            post_tool_use=[
                HookMatcher(
                    matcher="file_editor",
                    hooks=[HookDefinition(command=_HOOK_CMD, timeout=15)],
                )
            ]
        )

        _orig_remote_init = RemoteConversation.__init__

        def patched_remote_init(self_conv, *args, **kwargs):  # type: ignore[override]
            if "hook_config" not in kwargs or kwargs.get("hook_config") is None:
                kwargs["hook_config"] = GT_KERNEL_HOOK_CONFIG
            return _orig_remote_init(self_conv, *args, **kwargs)

        RemoteConversation.__init__ = patched_remote_init
        print("Patched RemoteConversation.__init__ with kernel hook_config")
    except Exception as exc:
        print(f"  WARN: could not patch RemoteConversation: {exc}")

    print(f"Patched SWEBenchEvaluation with gt_kernel_check.py (arm={ARM})", flush=True)
    return True


def patch_and_run() -> None:
    """Legacy CLI entry: patch + invoke the OH evaluator main() from sys.argv."""
    if not install_kernel_patch():
        sys.exit(1)
    try:
        from evaluation.benchmarks.swe_bench.run_infer import main  # type: ignore[import]
    except ImportError:
        from benchmarks.swebench.run_infer import main  # type: ignore[import]
    main()


def _extract_kernel_log(workspace, instance_id: str) -> None:
    """Pull edit-history + any kernel decision messages out of the container."""
    out_dir = os.environ.get("GT_KERNEL_LOG_DIR", "/tmp/gt_kernel_logs")
    os.makedirs(out_dir, exist_ok=True)
    try:
        res = workspace.execute_command(f"cat {EDIT_HISTORY} 2>/dev/null || echo ''")
        if res.stdout and res.stdout.strip():
            with open(os.path.join(out_dir, f"{instance_id}_edits.jsonl"), "w") as f:
                f.write(res.stdout)
            print(f"  edit history extracted: {instance_id}")
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
