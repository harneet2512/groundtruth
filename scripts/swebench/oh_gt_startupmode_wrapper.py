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
import sys  # noqa: F401 — used for sys.argv, sys.path, sys.exit

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

        # Step 3: Install bash hooks INSIDE the container.
        # The agent runs through the remote agent-server, not through
        # workspace.execute_command. We inject bash wrappers that the
        # agent's tmux session picks up transparently.

        # Install a PROMPT_COMMAND that runs gt check after edits
        hook_script = """
#!/bin/bash
# GT write-hook: runs after every command in the agent's shell
# Checks if any .py files were modified and runs quiet obligation check
GT_LAST_MTIME_FILE="/tmp/.gt_last_check_mtime"
REPO="/testbed"

# Get current mtime of modified files
current_mtime=$(stat -c %Y $(git -C "$REPO" diff --name-only 2>/dev/null | head -5 | sed "s|^|$REPO/|") 2>/dev/null | md5sum 2>/dev/null || echo "none")

if [ -f "$GT_LAST_MTIME_FILE" ]; then
    last_mtime=$(cat "$GT_LAST_MTIME_FILE" 2>/dev/null)
else
    last_mtime="none"
fi

if [ "$current_mtime" != "$last_mtime" ] && [ "$current_mtime" != "none" ]; then
    echo "$current_mtime" > "$GT_LAST_MTIME_FILE"
    output=$(cd "$REPO" && timeout 10 python3 /tmp/gt_tool.py check --quiet --max-items=3 2>/dev/null)
    if [ -n "$output" ]; then
        echo "$output"
    fi
fi
"""
        # Write the hook script
        workspace.execute_command(
            "cat > /tmp/gt_write_hook.sh << 'HOOKEOF'\n" + hook_script + "\nHOOKEOF\n"
            "chmod +x /tmp/gt_write_hook.sh"
        )

        # Install PROMPT_COMMAND in bashrc so it runs after every command
        if enable_write_hook:
            workspace.execute_command(
                'echo \'PROMPT_COMMAND="/tmp/gt_write_hook.sh"\' >> /root/.bashrc 2>/dev/null; '
                'echo \'PROMPT_COMMAND="/tmp/gt_write_hook.sh"\' >> /home/*/.bashrc 2>/dev/null; '
                'echo "GT write-hook installed"'
            )
            print(f"  Write-hook installed: {instance.id}")

        # For read-hook: create wrapper that appends structural notes to cat output
        if enable_read_hook:
            enrich_wrapper = """
#!/bin/bash
# GT read-hook: wraps cat to append structural coupling notes for .py files
/bin/cat "$@"
for arg in "$@"; do
    case "$arg" in
        *.py)
            if [ -f "$arg" ]; then
                output=$(cd /testbed && timeout 10 python3 /tmp/gt_tool.py enrich --file="$arg" 2>/dev/null)
                if [ -n "$output" ]; then
                    echo ""
                    echo "$output"
                fi
            fi
            ;;
    esac
done
"""
            workspace.execute_command(
                "cat > /usr/local/bin/cat << 'CATEOF'\n" + enrich_wrapper + "\nCATEOF\n"
                "chmod +x /usr/local/bin/cat"
            )
            print(f"  Read-hook installed: {instance.id}")

        # Step 4: Run the original evaluation with hooks installed
        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print(f"Patched SWEBenchEvaluation.evaluate_instance with GT v4 passive hooks")
    print()

    main()


if __name__ == "__main__":
    patch_and_run()
