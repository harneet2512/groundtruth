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

# Submission model label. NEVER hardcode the model — runs use whatever model the
# config sets (deepseek-v4-flash now; Qwen3-Coder previously). A stale hardcoded name
# mislabels the official submission. Derive it per-record from the run's own metadata
# (obj.metadata.llm_config.model), allow an explicit override, else a generic fallback.
_SUBMISSION_MODEL_OVERRIDE = os.environ.get("GT_SUBMISSION_MODEL_NAME", "").strip()


def _model_name_for(obj: dict) -> str:
    if _SUBMISSION_MODEL_OVERRIDE:
        return _SUBMISSION_MODEL_OVERRIDE
    model = (((obj.get("metadata") or {}).get("llm_config") or {}).get("model") or "").strip()
    base = "GroundTruth + OpenHands-CodeActAgent"
    return f"{base} + {model}" if model else base


def convert(input_path: str, output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)

    predictions = []
    contaminated = []
    empty_patches = []
    total = 0
    run_model = ""  # actual model from the run metadata (for the submission metadata block)

    with open(input_path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            iid = obj["instance_id"]
            total += 1

            raw_patch = obj.get("test_result", {}).get("git_patch", "") or ""
            patch = raw_patch.strip()

            # Patch integrity logging (P0-6)
            import hashlib
            _patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
            _patch_len = len(patch)
            # Malformed = patch cut mid-hunk. A trailing +/- line without a
            # source newline marker is suspicious because complete hunks should
            # continue with context or "\ No newline at end of file".
            _last_line = patch.rsplit("\n", 1)[-1] if patch else ""
            _patch_malformed = bool(
                patch and "diff --git" in patch
                and _last_line  # non-empty last segment after split
                and (
                    (
                        not raw_patch.endswith("\n")
                        and _last_line.startswith(("+", "-"))
                        and not _last_line.startswith(("+++", "---"))
                    )
                    or not _last_line.startswith(("diff ", "--- ", "+++ ", "@@ ", "+", "-", " ", "\\ "))
                )
            )
            print(f"[GT_PATCH_INTEGRITY] instance={iid} source=output.jsonl len={_patch_len} sha256={_patch_hash} malformed={_patch_malformed}", flush=True)
            if _patch_malformed:
                print(f"[GT_PATCH_INTEGRITY] WARNING: patch ends mid-line for {iid} — likely truncated", flush=True)

            # Check contamination
            for tag in GT_TAGS:
                if tag in patch:
                    contaminated.append({"instance_id": iid, "tag": tag})

            if not patch:
                empty_patches.append(iid)

            run_model = (((obj.get("metadata") or {}).get("llm_config") or {}).get("model") or "").strip() or run_model
            predictions.append({
                "instance_id": iid,
                "model_patch": patch,
                "model_name_or_path": _model_name_for(obj),
            })

    # Write predictions.jsonl
    pred_path = os.path.join(output_dir, "predictions.jsonl")
    with open(pred_path, "w", encoding="utf-8") as f:
        for p in predictions:
            line = json.dumps(p) + "\n"
            f.write(line)
            # Verify hash survives serialization (P0-6)
            import hashlib
            _written_patch = p["model_patch"]
            _written_hash = hashlib.sha256(_written_patch.encode("utf-8")).hexdigest()[:16]
            print(f"[GT_PATCH_INTEGRITY] instance={p['instance_id']} source=predictions.jsonl len={len(_written_patch)} sha256={_written_hash}", flush=True)

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
            # Derived from the run's own metadata — never hardcoded (was stale
            # "Qwen3-Coder-480B" while runs used deepseek-v4-flash).
            "name": _SUBMISSION_MODEL_OVERRIDE or run_model or "see run config",
            "submission_label": _model_name_for({"metadata": {"llm_config": {"model": run_model}}}),
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
