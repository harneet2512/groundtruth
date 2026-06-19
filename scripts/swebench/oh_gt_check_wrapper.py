#!/usr/bin/env python3
"""OpenHands wrapper that injects gt_tool.py into containers.

Patches evaluate_instance to write gt_tool.py into the container workspace
AFTER the container is started but BEFORE the conversation begins.

Uses chunked writing to avoid shell argument length limits.

Usage:
    python oh_gt_check_wrapper.py .llm_config/vertex_qwen3.json \
        --workspace docker \
        --prompt-path gt_check_only.j2 \
        --max-iterations 100 \
        --num-workers 2
"""

import base64
import os
import sys

# Ensure we can import from the benchmarks repo
sys.path.insert(0, os.getcwd())


def patch_and_run():
    """Monkey-patch SWEBenchEvaluation to inject gt_tool.py, then run main()."""

    # Read gt_tool.py source directly (not base64)
    gt_tool_path = os.environ.get(
        "GT_TOOL_PATH",
        os.path.expanduser("~/groundtruth/benchmarks/swebench/gt_tool.py"),
    )
    if not os.path.exists(gt_tool_path):
        print(f"ERROR: gt_tool.py not found at {gt_tool_path}")
        sys.exit(1)

    with open(gt_tool_path, "rb") as f:
        gt_tool_bytes = f.read()

    # Encode as base64 and split into 8KB chunks (safe for bash echo)
    gt_b64 = base64.b64encode(gt_tool_bytes).decode("ascii")
    CHUNK_SIZE = 8000
    chunks = [gt_b64[i : i + CHUNK_SIZE] for i in range(0, len(gt_b64), CHUNK_SIZE)]
    print(f"GT tool: {len(gt_tool_bytes)} bytes, {len(gt_b64)} base64, {len(chunks)} chunks")

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):
        """Inject gt_tool.py into container before running the agent."""
        # Write base64 in chunks
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_tool.b64")
            if res.exit_code != 0:
                print(f"WARNING: chunk {i} write failed: {res.stderr}")

        # Decode and make executable
        res = workspace.execute_command(
            "base64 -d /tmp/gt_tool.b64 > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py && echo GT_READY"
        )
        if "GT_READY" in (res.stdout or ""):
            print(f"GT tool injected into container for {instance.id}")
        else:
            print(f"WARNING: GT tool injection may have failed for {instance.id}: {res.stderr}")

        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print("Patched SWEBenchEvaluation.evaluate_instance with GT tool injection")

    main()


if __name__ == "__main__":
    patch_and_run()
