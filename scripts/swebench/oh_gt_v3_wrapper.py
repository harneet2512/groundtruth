#!/usr/bin/env python3
"""OpenHands wrapper for v3 3-endpoint GT tool.

Injects gt_tool_v3.py (~55KB) into SWE-bench containers.
Chunked base64 injection: safe for bash on any VM.

Three commands available inside container:
    python3 /tmp/gt_tool.py impact <Symbol>
    python3 /tmp/gt_tool.py references <Symbol>
    python3 /tmp/gt_tool.py check

Usage:
    GT_TOOL_PATH=~/groundtruth/benchmarks/swebench/gt_tool_v3.py \
    python oh_gt_v3_wrapper.py .llm_config/vertex_qwen3.json \
        --workspace docker \
        --prompt-path gt_v3_hardgate.j2 \
        --max-iterations 100 \
        --num-workers 4
"""

import base64
import os
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_DEFAULT_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_v3.py")
_FALLBACK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool.py")


def patch_and_run():
    """Monkey-patch SWEBenchEvaluation to inject gt_tool_v3, then run main()."""

    gt_tool_path = os.environ.get("GT_TOOL_PATH", _DEFAULT_TOOL)
    if not os.path.exists(gt_tool_path):
        if os.path.exists(_FALLBACK_TOOL):
            print(f"WARNING: v3 tool not found at {gt_tool_path}")
            print(f"         Falling back to: {_FALLBACK_TOOL}")
            gt_tool_path = _FALLBACK_TOOL
        else:
            print(f"ERROR: No GT tool found at {gt_tool_path} or {_FALLBACK_TOOL}")
            sys.exit(1)

    with open(gt_tool_path, "rb") as f:
        gt_tool_bytes = f.read()

    gt_b64 = base64.b64encode(gt_tool_bytes).decode("ascii")
    CHUNK_SIZE = 8000
    chunks = [gt_b64[i : i + CHUNK_SIZE] for i in range(0, len(gt_b64), CHUNK_SIZE)]
    print(f"GT tool v3: {os.path.basename(gt_tool_path)}")
    print(f"  Source: {len(gt_tool_bytes):,} bytes")
    print(f"  Base64: {len(gt_b64):,} bytes, {len(chunks)} chunks")

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):
        """Inject gt_tool_v3.py into container before running the agent."""
        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_tool.b64")
            if res.exit_code != 0:
                print(f"WARNING: chunk {i}/{len(chunks)} write failed for {instance.id}: {res.stderr}")
                ok = False
                break

        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_tool.b64 > /tmp/gt_tool.py && "
                "chmod +x /tmp/gt_tool.py && "
                "rm -f /tmp/gt_tool.b64 && "
                "echo GT_V3_READY"
            )
            if "GT_V3_READY" in (res.stdout or ""):
                print(f"  GT v3 injected: {instance.id}")
            else:
                print(f"  WARNING: GT v3 injection uncertain for {instance.id}: {res.stderr}")
        else:
            print(f"  WARNING: GT v3 injection FAILED for {instance.id}")

        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print("Patched SWEBenchEvaluation.evaluate_instance with GT v3 tool injection")
    print()

    main()


if __name__ == "__main__":
    patch_and_run()
