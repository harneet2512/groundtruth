"""Convert OpenHands output.jsonl → SWE-bench-Live Lite submission format.

Input:  OH output.jsonl (one JSON object per line with instance_id, test_result.git_patch, history, etc.)
Output: predictions.jsonl (one JSON object per line: instance_id, model_patch, model_name_or_path)

Also generates:
  - submission_metadata.json (full config, GT commit, method description)
  - contamination_report.txt (checks for GT artifacts in patches)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

GT_TAGS = [
    "gt-evidence", "gt-advisory", "gt-task-brief", "gt-prefetch",
    "GT_CONTRACT", "GT_OK", "GT_REDIRECT", "GT_CONTEXT", "GT_GATE",
    "GT_STATUS", "BRIEFED_FILE", "UNBRIEFED_FILE", "GT_PATCH_SHAPE",
    "[NOTE] Callers", "[issue-relevant]",
]

MODEL_NAME = "GroundTruth + OpenHands-CodeActAgent + Qwen3-Coder-480B-A35B-Instruct"


def convert(input_path: str, output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)

    predictions = []
    contaminated = []
    empty_patches = []
    total = 0

    with open(input_path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            iid = obj["instance_id"]
            total += 1

            patch = obj.get("test_result", {}).get("git_patch", "") or ""
            patch = patch.strip()

            # Check contamination
            for tag in GT_TAGS:
                if tag in patch:
                    contaminated.append({"instance_id": iid, "tag": tag})

            if not patch:
                empty_patches.append(iid)

            predictions.append({
                "instance_id": iid,
                "model_patch": patch,
                "model_name_or_path": MODEL_NAME,
            })

    # Write predictions.jsonl
    pred_path = os.path.join(output_dir, "predictions.jsonl")
    with open(pred_path, "w", encoding="utf-8") as f:
        for p in predictions:
            f.write(json.dumps(p) + "\n")

    # Write contamination report
    report_path = os.path.join(output_dir, "contamination_report.txt")
    with open(report_path, "w") as f:
        f.write(f"Total predictions: {total}\n")
        f.write(f"Empty patches: {len(empty_patches)}\n")
        f.write(f"Contaminated patches: {len(contaminated)}\n\n")
        if contaminated:
            f.write("CONTAMINATION FOUND:\n")
            for c in contaminated:
                f.write(f"  {c['instance_id']}: contains '{c['tag']}'\n")
        else:
            f.write("NO CONTAMINATION FOUND — all patches are clean.\n")
        if empty_patches:
            f.write(f"\nEmpty patches ({len(empty_patches)}):\n")
            for e in empty_patches:
                f.write(f"  {e}\n")

    # Write submission metadata
    gt_commit = "unknown"
    try:
        gt_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True,
        ).strip()
    except Exception:
        pass

    gt_branch = "unknown"
    try:
        gt_branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True,
        ).strip()
    except Exception:
        pass

    metadata = {
        "submission": {
            "benchmark": "SWE-bench-Live Lite",
            "dataset": "SWE-bench-Live/SWE-bench-Live",
            "split": "lite",
            "total_predictions": total,
            "non_empty_patches": total - len(empty_patches),
            "contamination_count": len(contaminated),
        },
        "model": {
            "name": "Qwen3-Coder-480B-A35B-Instruct",
            "provider": "Vertex AI MaaS (global endpoint)",
            "temperature": 0.7,
            "top_p": 1.0,
            "top_k": 20,
            "repetition_penalty": 1.05,
            "max_output_tokens": 8192,
            "reasoning_effort": "high",
        },
        "agent": {
            "framework": "OpenHands v0.54.0",
            "agent_class": "CodeActAgent",
            "max_iterations": 100,
            "llm_call_cap": 150,
            "condenser": "noop",
        },
        "augmentation": {
            "name": "GroundTruth",
            "version": "general_start",
            "gt_commit": gt_commit,
            "gt_branch": gt_branch,
            "gt_phase": "full",
            "layers_active": "L1 (brief) + L3 (post-edit evidence) + L3b (post-view navigation) + L5 (stuck-pattern advisory) + L6 (incremental reindex)",
            "llm_calls_in_gt": 0,
            "description": (
                "GroundTruth is a deterministic codebase intelligence layer that "
                "indexes source code into a SQLite call graph using tree-sitter, "
                "then provides evidence-based briefings and reactive evidence to "
                "the AI coding agent at action boundaries. Zero LLM calls in the "
                "augmentation pipeline. All signals are computed from the call graph, "
                "BM25 lexical search, and git history."
            ),
            "key_mechanisms": [
                "Pre-task brief: ranked candidate files (BM25 + graph reach + confidence gating)",
                "Post-edit: caller code lines, function signatures, sibling patterns from call graph",
                "Post-view: graph navigation hints (callers, callees, issue-relevant neighbors)",
                "Stuck-pattern advisory: detects scaffolding loops, never names specific files",
                "Scaffold strip: removes scaffold files (reproduce_*, debug_*, temp_*) before patch capture, matching SWE-agent submit behavior; non-scaffold new files are preserved",
                "Task-relevance annotation: callers tagged with issue keyword overlap",
                "Cross-domain bridging: co-change expansion when symptom convergence detected",
            ],
            "what_gt_does_NOT_do": [
                "No access to gold patches or test results at inference time",
                "No task-specific conditionals or per-repo tuning",
                "No LLM calls for retrieval, ranking, or evidence generation",
                "No modification of patches after git diff capture",
            ],
        },
    }

    meta_path = os.path.join(output_dir, "submission_metadata.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)

    # Print summary
    print(f"Predictions:    {pred_path} ({total} tasks)")
    print(f"Empty patches:  {len(empty_patches)}")
    print(f"Contaminated:   {len(contaminated)}")
    print(f"Metadata:       {meta_path}")
    print(f"Report:         {report_path}")

    if contaminated:
        print("\n*** WARNING: CONTAMINATION FOUND — patches flagged above ***")
        print("Eval will proceed — contaminated patches will fail naturally.")


def main():
    parser = argparse.ArgumentParser(description="Convert OH output to SWE-bench submission format")
    parser.add_argument("input", help="Path to OH output.jsonl")
    parser.add_argument("--output-dir", default="submission", help="Output directory")
    args = parser.parse_args()
    convert(args.input, args.output_dir)


if __name__ == "__main__":
    main()
