#!/usr/bin/env python3
"""OpenHands wrapper for v4 passive hook GT tool.

Injects gt_tool_v4.py into SWE-bench containers and uses OpenHands'
native HookConfig (post_tool_use) to transparently run GT checks after
file edits. The agent never knows GT exists.

Usage:
    python oh_gt_startupmode_wrapper.py .llm_config/vertex_qwen3.json \
        --workspace docker \
        --prompt-path gt_startupmode.j2 \
        --max-iterations 100 \
        --num-workers 5 \
        --hooks write-only   # or --hooks both
"""

import base64
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_DEFAULT_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_v4.py")
_FALLBACK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_v3.py")


def patch_and_run():
    """Monkey-patch SWEBenchEvaluation to inject gt_tool_v4 with hooks."""

    # Parse --hooks flag from argv
    hooks_mode = 'write-only'  # default: Experiment A
    filtered_argv = []
    for arg in sys.argv[1:]:
        if arg.startswith('--hooks='):
            hooks_mode = arg.split('=', 1)[1]
        else:
            filtered_argv.append(arg)
    sys.argv = [sys.argv[0]] + filtered_argv

    enable_read_hook = hooks_mode in ('both', 'read-write', 'all')
    enable_write_hook = hooks_mode in ('write-only', 'both', 'read-write', 'all')

    print(f"GT startupmode hooks: {hooks_mode}")
    print(f"  Read hook (enrich):     {'ON' if enable_read_hook else 'OFF'}")
    print(f"  Write hook (check):     {'ON' if enable_write_hook else 'OFF'}")

    # Load gt_tool
    gt_tool_path = os.environ.get("GT_TOOL_PATH", _DEFAULT_TOOL)
    if not os.path.exists(gt_tool_path):
        if os.path.exists(_FALLBACK_TOOL):
            print(f"WARNING: v4 tool not found at {gt_tool_path}")
            print(f"         Falling back to: {_FALLBACK_TOOL}")
            gt_tool_path = _FALLBACK_TOOL
        else:
            print(f"ERROR: No GT tool found at {gt_tool_path} or {_FALLBACK_TOOL}")
            sys.exit(1)

    with open(gt_tool_path, "rb") as f:
        gt_tool_bytes = f.read()

    gt_b64 = base64.b64encode(gt_tool_bytes).decode("ascii")
    CHUNK_SIZE = 8000
    chunks = [gt_b64[i: i + CHUNK_SIZE] for i in range(0, len(gt_b64), CHUNK_SIZE)]
    print(f"GT tool v4: {os.path.basename(gt_tool_path)}")
    print(f"  Source: {len(gt_tool_bytes):,} bytes")
    print(f"  Base64: {len(gt_b64):,} bytes, {len(chunks)} chunks")

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main
    from openhands.sdk.hooks.config import HookConfig, HookMatcher, HookDefinition

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):
        """Inject gt_tool_v4.py, pre-build index, configure hooks."""

        # Step 1: Inject gt_tool via base64 chunks
        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_tool.b64")
            if res.exit_code != 0:
                print(f"WARNING: chunk {i}/{len(chunks)} write failed for {instance.id}")
                ok = False
                break

        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_tool.b64 > /tmp/gt_tool.py && "
                "chmod +x /tmp/gt_tool.py && "
                "rm -f /tmp/gt_tool.b64 && "
                "echo GT_V4_READY"
            )
            if "GT_V4_READY" in (res.stdout or ""):
                print(f"  GT v4 injected: {instance.id}")
            else:
                print(f"  WARNING: GT v4 injection uncertain for {instance.id}")
        else:
            print(f"  WARNING: GT v4 injection FAILED for {instance.id}")
            return _original_evaluate(self, instance, workspace)

        # Step 2: Pre-build index
        res = workspace.execute_command(
            "cd /testbed && timeout 45 python3 /tmp/gt_tool.py --build-index 2>/dev/null || true"
        )
        if res.stdout and "INDEX_READY" in res.stdout:
            print(f"  Index pre-built: {instance.id} -- {res.stdout.strip()}")
        else:
            print(f"  Index pre-build: no output (may have timed out): {instance.id}")

        # Step 3: Configure HookConfig for post_tool_use hooks
        # OpenHands' native hook system runs commands after tool execution
        # and appends their output to the tool result the agent sees.
        hooks = []

        if enable_write_hook:
            # Fire after file_editor (str_replace, create, insert) and terminal
            hooks.append(
                HookMatcher(
                    matcher="file_editor",
                    hooks=[HookDefinition(
                        command="cd /testbed && python3 /tmp/gt_tool.py check --quiet --max-items=3 2>/dev/null || true",
                        timeout=15,
                    )]
                )
            )

        if enable_read_hook:
            # Fire after file_editor view
            hooks.append(
                HookMatcher(
                    matcher="file_editor",
                    hooks=[HookDefinition(
                        command="cd /testbed && python3 /tmp/gt_tool.py enrich --file=$OPENHANDS_FILE_PATH 2>/dev/null || true",
                        timeout=15,
                    )]
                )
            )

        if hooks:
            hook_config = HookConfig(post_tool_use=hooks)
            # Inject hook_config into the metadata so Conversation picks it up
            if not hasattr(self.metadata, '_gt_hook_config'):
                self.metadata._gt_hook_config = hook_config
            print(f"  Hooks configured: {len(hooks)} post_tool_use matchers")

        # Step 4: Run original evaluation
        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate

    # Patch Conversation.__new__ (factory) to inject hook_config
    # Conversation uses __new__ as a factory that creates Local/RemoteConversation
    try:
        from openhands.sdk.conversation import Conversation

        _orig_conv_new = Conversation.__new__

        def patched_conv_new(cls, *args, **kwargs):
            # Inject hook_config if not already set
            if 'hook_config' not in kwargs or kwargs['hook_config'] is None:
                post_hooks = []
                if enable_write_hook:
                    post_hooks.append(
                        HookMatcher(
                            matcher="file_editor",
                            hooks=[HookDefinition(
                                command="cd /testbed && python3 /tmp/gt_tool.py check --quiet --max-items=3 2>/dev/null || true",
                                timeout=15,
                            )]
                        )
                    )
                if post_hooks:
                    kwargs['hook_config'] = HookConfig(post_tool_use=post_hooks)
            return _orig_conv_new(cls, *args, **kwargs)

        Conversation.__new__ = patched_conv_new
        print("Patched Conversation.__new__ to inject GT hook_config")
    except Exception as e:
        print(f"  WARNING: Could not patch Conversation: {e}")
        import traceback
        traceback.print_exc()

    print(f"Patched SWEBenchEvaluation.evaluate_instance with GT v4 passive hooks")
    print()

    main()


if __name__ == "__main__":
    patch_and_run()
