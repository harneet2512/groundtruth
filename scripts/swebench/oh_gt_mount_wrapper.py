#!/usr/bin/env python3
"""OpenHands wrapper for leaderboard benchmark — injects gt_tool_check_only.py.

Uses the stripped check-only GT tool (~34KB) instead of the full gt_tool.py (~108KB).
Chunked base64 injection: ~6 chunks of 8KB each, safe for bash on any VM.

Usage:
    GT_TOOL_PATH=~/groundtruth/benchmarks/swebench/gt_tool_check_only.py \
    python oh_gt_mount_wrapper.py .llm_config/vertex_qwen3.json \
        --workspace docker \
        --prompt-path gt_check_hardgate.j2 \
        --max-iterations 100 \
        --num-workers 4
"""

import base64
import os
import sys

# Ensure we can import from the benchmarks repo
sys.path.insert(0, os.getcwd())

# Default to check-only tool; fall back to full tool
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_DEFAULT_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_check_only.py")
_FALLBACK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool.py")


def patch_and_run():
    """Monkey-patch SWEBenchEvaluation to inject gt_tool, then run main()."""

    gt_tool_path = os.environ.get("GT_TOOL_PATH", _DEFAULT_TOOL)
    if not os.path.exists(gt_tool_path):
        if os.path.exists(_FALLBACK_TOOL):
            print(f"WARNING: Check-only tool not found at {gt_tool_path}")
            print(f"         Falling back to full tool: {_FALLBACK_TOOL}")
            gt_tool_path = _FALLBACK_TOOL
        else:
            print(f"ERROR: No GT tool found at {gt_tool_path} or {_FALLBACK_TOOL}")
            sys.exit(1)

    with open(gt_tool_path, "rb") as f:
        gt_tool_bytes = f.read()

    gt_b64 = base64.b64encode(gt_tool_bytes).decode("ascii")
    CHUNK_SIZE = 8000  # 8KB chunks, safe for bash echo
    chunks = [gt_b64[i : i + CHUNK_SIZE] for i in range(0, len(gt_b64), CHUNK_SIZE)]
    print(f"GT tool: {os.path.basename(gt_tool_path)}")
    print(f"  Source: {len(gt_tool_bytes):,} bytes")
    print(f"  Base64: {len(gt_b64):,} bytes, {len(chunks)} chunks")

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):
        """Inject gt_tool.py into container before running the agent."""
        # Write base64 in chunks
        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_tool.b64")
            if res.exit_code != 0:
                print(f"WARNING: chunk {i}/{len(chunks)} write failed for {instance.id}: {res.stderr}")
                ok = False
                break

        if ok:
            # Decode and make executable
            res = workspace.execute_command(
                "base64 -d /tmp/gt_tool.b64 > /tmp/gt_tool.py && "
                "chmod +x /tmp/gt_tool.py && "
                "rm -f /tmp/gt_tool.b64 && "
                "echo GT_READY"
            )
            if "GT_READY" in (res.stdout or ""):
                print(f"  GT injected: {instance.id}")
            else:
                print(f"  WARNING: GT injection uncertain for {instance.id}: {res.stderr}")
        else:
            print(f"  WARNING: GT injection FAILED for {instance.id} — agent will run without GT")

        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print("Patched SWEBenchEvaluation.evaluate_instance with GT tool injection")
    print()

    main()


if __name__ == "__main__":
    patch_and_run()
