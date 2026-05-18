"""Convert Inspect AI eval logs → SWE-bench predictions.jsonl format.

Reads .eval files from an Inspect log directory, extracts git patches
from the passthrough scorer's answer field, and writes predictions.jsonl
compatible with the Microsoft SWE-bench-Live evaluation harness.

Usage:
    python scripts/swebench/convert_inspect_to_predictions.py \
        --log-dir /tmp/inspect_logs \
        --output-dir /tmp/predictions
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def convert(log_dir: str, output_dir: str, model_name: str = "DeepSeek-V4-Flash-Inspect-Baseline") -> None:
    os.makedirs(output_dir, exist_ok=True)

    try:
        from inspect_ai.log import list_eval_logs, read_eval_log
    except ImportError:
        print("FATAL: inspect-ai not installed", file=sys.stderr)
        sys.exit(1)

    predictions = []
    empty = 0
    total = 0

    logs = list_eval_logs(log_dir)
    for log_path in logs:
        log = read_eval_log(str(log_path))
        for sample in log.samples or []:
            total += 1
            instance_id = sample.id or "unknown"

            patch = ""
            if sample.scores:
                for _, score_obj in sample.scores.items():
                    if score_obj.metadata and score_obj.metadata.get("full_diff"):
                        patch = score_obj.metadata["full_diff"]
                    elif score_obj.answer and "diff --git" in (score_obj.answer or ""):
                        patch = score_obj.answer

            if not patch:
                empty += 1

            predictions.append({
                "instance_id": instance_id,
                "model_patch": patch,
                "model_name_or_path": model_name,
            })

    pred_path = os.path.join(output_dir, "predictions.jsonl")
    with open(pred_path, "w") as f:
        for p in predictions:
            f.write(json.dumps(p) + "\n")

    print(f"Converted {total} samples ({total - empty} with patches, {empty} empty)")
    print(f"Output: {pred_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-name", default="DeepSeek-V4-Flash-Inspect-Baseline")
    args = parser.parse_args()
    convert(args.log_dir, args.output_dir, args.model_name)
