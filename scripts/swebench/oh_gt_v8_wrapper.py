#!/usr/bin/env python3
"""OpenHands wrapper for GT v8 — active tool injection via system prompt.

v7 lesson: passive post-edit hooks fire reliably but the agent never calls
the active `understand` command because the instructions are in the instance
prompt (user message), not the system prompt.

v8 fix: Inject GT tool instructions into the agent's system message via
system_prompt_kwargs, so the agent treats GT as a first-class tool.

Architecture:
1. Monkey-patch SWEBenchEvaluation.evaluate_instance
2. Inject gt_hook.py into each Docker container (chunked base64)
3. Override Agent() creation to include GT system prompt instructions
4. After task, extract /tmp/gt_hook_log.jsonl for analysis

Usage:
    cd /root/oh-benchmarks
    .venv/bin/python /path/to/oh_gt_v8_wrapper.py <llm_config.json> \
        --workspace docker --max-iterations 50 --num-workers 4 \
        --prompt-path gt_hook_v7.j2 --output-dir <dir> --note v8_gt
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_HOOK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_hook.py")

# ── GT system prompt instructions ─────────────────────────────────────
# This gets injected into the agent's system message so it treats GT
# as a core capability, not an optional footnote in the user message.
GT_SYSTEM_INSTRUCTIONS = """
<gt_codebase_intelligence>
You have a codebase intelligence tool that shows cross-file structural data
you CANNOT get from reading files or grep.

## Command (use EARLY, use SPARINGLY):
  python3 /tmp/gt_hook.py understand <filepath> --root=/workspace --quiet --max-lines=10

Returns: cross-file callers, test file locations, sibling method norms, behavioral contracts.

## STRICT BUDGET — 3 calls max:
- Call understand on exactly 1-3 key files ONCE during initial exploration.
- NEVER call understand more than 3 times total. After 3 calls, STOP exploring and START fixing.
- NEVER re-run understand on a file you already analyzed.
- If you find yourself wanting to call it a 4th time, you already have enough context. Write the fix.

## After editing, run verify ONCE:
  python3 /tmp/gt_hook.py verify --root=/workspace --quiet --max-items=3

## Workflow:
1. Read the issue. Grep/find to locate the relevant file(s).
2. Run understand on 1-3 key files to get callers and norms.
3. Write your fix, informed by the cross-file context.
4. Run verify once. Run tests. Submit.

Do NOT loop between understand and editing. Get the context once, then commit to a fix.
</gt_codebase_intelligence>
"""


def patch_and_run() -> None:
    """Load gt_hook.py, patch Agent creation + evaluate_instance, run main."""

    # ── Load hook file ────────────────────────────────────────────────
    if not os.path.exists(_HOOK_TOOL):
        print(f"ERROR: gt_hook.py not found at {_HOOK_TOOL}")
        sys.exit(1)

    with open(_HOOK_TOOL, "rb") as fh:
        hook_bytes = fh.read()

    gt_b64 = base64.b64encode(hook_bytes).decode("ascii")
    CHUNK = 50_000
    chunks = [gt_b64[i: i + CHUNK] for i in range(0, len(gt_b64), CHUNK)]

    print(f"gt_hook.py: {len(hook_bytes):,} bytes | {len(gt_b64):,} b64 | {len(chunks)} chunks")

    # ── Import OpenHands ──────────────────────────────────────────────
    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main
    from openhands.sdk import Agent

    _original_evaluate = SWEBenchEvaluation.evaluate_instance
    _original_agent_init = Agent.__init__

    # ── Patch 1: Inject GT instructions into Agent system prompt ──────
    def patched_agent_init(self, *args, **kwargs):
        """Add GT tool instructions to system_prompt_kwargs."""
        spk = kwargs.get("system_prompt_kwargs", {}) or {}

        # Append GT instructions to any existing llm_security_analyzer content
        # The system_prompt.j2 template renders system_prompt_kwargs values
        # We add a custom key that we'll also inject via dynamic context
        spk["cli_mode"] = True
        kwargs["system_prompt_kwargs"] = spk

        _original_agent_init(self, *args, **kwargs)

        # After init, append GT instructions to the dynamic context
        # by modifying the agent's _static_system_message cache
        if hasattr(self, '_AgentBase__static_system_message') or hasattr(self, '_static_system_message_cache'):
            pass  # Will use dynamic context approach instead

    Agent.__init__ = patched_agent_init

    # ── Patch 2: Inject gt_hook.py + GT system context ────────────────
    def patched_evaluate(self, instance, workspace):
        instance_id = getattr(instance, "id", str(instance))
        print(f">>> GT v8: patched_evaluate for {instance_id}", flush=True)

        # Step 1: Inject gt_hook.py via chunked base64
        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
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

        # Step 2: Run the original evaluate (which creates Agent + Conversation)
        # But first, we monkey-patch the Conversation.send_message to prepend
        # GT context to the first user message
        _orig_send = None
        _Conversation = None
        try:
            from openhands.sdk import Conversation as _Conv
            _Conversation = _Conv
            _orig_send = _Conversation.send_message

            _first_message_patched = [False]

            def patched_send_message(conv_self, message, *args, **kwargs):
                """Prepend GT system instructions to the first user message."""
                if not _first_message_patched[0]:
                    _first_message_patched[0] = True
                    gt_preamble = GT_SYSTEM_INSTRUCTIONS.strip()
                    message = gt_preamble + "\n\n" + message
                    print(f"  GT v8: Injected {len(gt_preamble)} chars into first message for {instance_id}")
                return _orig_send(conv_self, message, *args, **kwargs)

            _Conversation.send_message = patched_send_message
        except Exception as e:
            print(f"  WARNING: Could not patch Conversation.send_message: {e}")

        try:
            result = _original_evaluate(self, instance, workspace)
        finally:
            # Restore original send_message
            if _Conversation is not None and _orig_send is not None:
                _Conversation.send_message = _orig_send
            # Extract hook logs
            _extract_hook_log(workspace, instance_id)

        return result

    SWEBenchEvaluation.evaluate_instance = patched_evaluate

    # ── Verify patches ────────────────────────────────────────────────
    print("GT v8: Patches applied")
    print(f"  evaluate_instance patched: {SWEBenchEvaluation.evaluate_instance is patched_evaluate}")
    print(flush=True)

    main()


def _extract_hook_log(workspace, instance_id: str) -> None:
    """Copy /tmp/gt_hook_log.jsonl out of the container."""
    out_dir = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")
    os.makedirs(out_dir, exist_ok=True)
    try:
        res = workspace.execute_command("cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''")
        if res.stdout and res.stdout.strip():
            log_path = os.path.join(out_dir, f"{instance_id}.jsonl")
            with open(log_path, "w") as fh:
                fh.write(res.stdout)
            print(f"  hook JSONL extracted: {instance_id} ({len(res.stdout)} bytes)")

        res2 = workspace.execute_command("cat /tmp/gt_hook_stdout.log 2>/dev/null || echo ''")
        if res2.stdout and res2.stdout.strip():
            stdout_path = os.path.join(out_dir, f"{instance_id}_stdout.log")
            with open(stdout_path, "w") as fh:
                fh.write(res2.stdout)
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
