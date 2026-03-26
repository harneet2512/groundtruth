#!/usr/bin/env python3
"""OpenHands wrapper for v4 passive hook GT tool.

Injects gt_tool_v4.py into SWE-bench containers and hooks into
workspace.execute_command to transparently enrich file reads and
check file edits. The agent never knows GT exists.

Two hooks:
    Read-hook:  After file view/cat/read → appends structural coupling notes
    Write-hook: After file edit/write → appends obligation check results

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
import re
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_DEFAULT_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_v4.py")
_FALLBACK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_v3.py")


# ═══════════════════════════════════════
# Command detection helpers
# ═══════════════════════════════════════

# Patterns that indicate a file-read command
_READ_PATTERNS = [
    # cat <path>, head <path>, tail <path>, less <path>
    re.compile(r'(?:^|\s)(?:cat|head|tail|less|more)\s+(?:-[^\s]*\s+)*([^\s|><;]+\.py)\b'),
    # sed -n '...' <path> (read-only sed)
    re.compile(r'(?:^|\s)sed\s+-n\s+[^\s]+\s+([^\s|><;]+\.py)\b'),
]

# Patterns that indicate a file-edit command
_EDIT_PATTERNS = [
    re.compile(r'(?:^|\s)sed\s+-i'),           # sed -i (in-place edit)
    re.compile(r'(?:^|\s)patch\b'),             # patch command
    re.compile(r'>\s*[^\s]+\.py\b'),            # redirect to .py file
    re.compile(r'str_replace'),                 # OpenHands str_replace_editor
    re.compile(r'insert'),                      # OpenHands insert command
    re.compile(r'create'),                      # OpenHands create command
]


def _extract_read_path(cmd):
    """Extract .py file path from a file-read command. Returns path or None."""
    if not cmd or not isinstance(cmd, str):
        return None
    for pattern in _READ_PATTERNS:
        match = pattern.search(cmd)
        if match:
            path = match.group(1)
            if path.endswith('.py') and not path.startswith('-'):
                return path
    return None


def _is_edit_command(cmd):
    """Detect if a command is a file-edit operation."""
    if not cmd or not isinstance(cmd, str):
        return False
    for pattern in _EDIT_PATTERNS:
        if pattern.search(cmd):
            return True
    return False


# ═══════════════════════════════════════
# Main patch logic
# ═══════════════════════════════════════

def patch_and_run():
    """Monkey-patch SWEBenchEvaluation to inject gt_tool_v4 with hooks."""

    # Parse --hooks flag from argv
    hooks_mode = 'write-only'  # default: Experiment A
    filtered_argv = []
    for arg in sys.argv[1:]:
        if arg.startswith('--hooks='):
            hooks_mode = arg.split('=', 1)[1]
        elif arg == '--hooks':
            # Next arg is the value — handle later
            pass
        else:
            filtered_argv.append(arg)
    # Restore sys.argv without --hooks for OpenHands
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

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):
        """Inject gt_tool_v4.py, pre-build index, install hooks."""

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

        # Step 2: Pre-build index (runs in ~20-30s, cached for all subsequent calls)
        res = workspace.execute_command(
            "cd /testbed && timeout 45 python3 /tmp/gt_tool.py --build-index 2>/dev/null || true"
        )
        if res.stdout and "INDEX_READY" in res.stdout:
            print(f"  Index pre-built: {instance.id} — {res.stdout.strip()}")
        else:
            print(f"  Index pre-build: no output (may have timed out): {instance.id}")

        # Step 3: Install hooks by wrapping workspace.execute_command
        _original_execute = workspace.execute_command

        def hooked_execute(cmd, **kwargs):
            result = _original_execute(cmd, **kwargs)

            # Read-hook: enrich file views with structural coupling notes
            if enable_read_hook and result.exit_code == 0:
                file_path = _extract_read_path(cmd)
                if file_path and file_path.endswith('.py'):
                    try:
                        enrich = _original_execute(
                            f"cd /testbed && timeout 10 python3 /tmp/gt_tool.py enrich --file={file_path} 2>/dev/null",
                            **kwargs
                        )
                        if enrich.stdout and enrich.stdout.strip():
                            result.stdout = (result.stdout or "") + "\n\n" + enrich.stdout.strip()
                    except Exception:
                        pass  # silent on failure

            # Write-hook: check obligations after file edits
            if enable_write_hook and result.exit_code == 0 and _is_edit_command(cmd):
                try:
                    check = _original_execute(
                        "cd /testbed && timeout 10 python3 /tmp/gt_tool.py check --quiet --max-items=3 2>/dev/null",
                        **kwargs
                    )
                    if check.stdout and check.stdout.strip():
                        result.stdout = (result.stdout or "") + "\n" + check.stdout.strip()
                except Exception:
                    pass  # silent on failure

            return result

        workspace.execute_command = hooked_execute

        # Step 4: Run the original evaluation with hooks installed
        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print(f"Patched SWEBenchEvaluation.evaluate_instance with GT v4 passive hooks")
    print()

    main()


if __name__ == "__main__":
    patch_and_run()
