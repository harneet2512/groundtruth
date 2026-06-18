#!/usr/bin/env python3
"""Prepare GT predictions and invoke the official SWE-bench Pro evaluator."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_patch_from_obj(obj: Any, instance_id: str) -> str:
    if isinstance(obj, dict):
        if instance_id in obj:
            patch = extract_patch_from_obj(obj[instance_id], instance_id)
            if patch:
                return patch
        for key in ("model_patch", "patch", "diff"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in obj.values():
            patch = extract_patch_from_obj(value, instance_id)
            if patch:
                return patch
    elif isinstance(obj, list):
        for item in obj:
            patch = extract_patch_from_obj(item, instance_id)
            if patch:
                return patch
    return ""


def find_patch_text(preds: Path, instance_id: str) -> str:
    for candidate in sorted(preds.parent.rglob("*")):
        if candidate.is_file() and candidate.suffix in {".patch", ".diff"} and candidate.stat().st_size:
            return candidate.read_text(encoding="utf-8", errors="replace")
    try:
        return extract_patch_from_obj(load_json(preds), instance_id)
    except Exception:
        return ""


def write_patch_json(output_dir: Path, instance_id: str, patch_text: str) -> Path:
    patch_path = output_dir / "official_patches.json"
    payload = [{"instance_id": instance_id, "patch": patch_text, "prefix": "gt"}]
    patch_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return patch_path


def load_dataset_row(instance_id: str) -> dict[str, Any]:
    old_env = {
        key: os.environ.get(key)
        for key in ("HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_OFFLINE")
    }
    for key in old_env:
        os.environ.pop(key, None)
    try:
        from datasets import load_dataset

        ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
        for row in ds:
            if row.get("instance_id") == instance_id:
                return dict(row)
    finally:
        for key, value in old_env.items():
            if value is not None:
                os.environ[key] = value
    raise RuntimeError(f"{instance_id} not found in ScaleAI/SWE-bench_Pro test split")


def write_raw_sample_csv(output_dir: Path, instance_id: str) -> Path:
    row = load_dataset_row(instance_id)
    if "FAIL_TO_PASS" in row and "fail_to_pass" not in row:
        row["fail_to_pass"] = row["FAIL_TO_PASS"]
    if "PASS_TO_PASS" in row and "pass_to_pass" not in row:
        row["pass_to_pass"] = row["PASS_TO_PASS"]
    raw_path = output_dir / "raw_sample.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)
    return raw_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preds", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--dockerhub-tag", required=True)
    parser.add_argument("--pro-os-dir", default="swebench-pro-os", type=Path)
    parser.add_argument("--output-dir", default="trial_results/pro_eval", type=Path)
    parser.add_argument("--reward-file", default="trial_results/reward.txt", type=Path)
    parser.add_argument("--trial-log", default="trial_output.log", type=Path)
    parser.add_argument("--num-workers", type=int, default=1)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.reward_file.parent.mkdir(parents=True, exist_ok=True)
    eval_script = args.pro_os_dir / "swe_bench_pro_eval.py"
    scripts_dir = args.pro_os_dir / "run_scripts"
    run_script = scripts_dir / args.instance_id / "run_script.sh"
    parser_script = scripts_dir / args.instance_id / "parser.py"
    summary_path = args.output_dir / "pro_eval_summary.json"

    def finish(
        reward: int,
        status: str,
        detail: str,
        returncode: int | None = None,
        exit_code: int = 0,
    ) -> int:
        args.reward_file.write_text(f"{reward}\n", encoding="utf-8")
        summary = {
            "instance_id": args.instance_id,
            "reward": reward,
            "status": status,
            "detail": detail,
            "returncode": returncode,
            "verifier": str(eval_script),
            "run_script": str(run_script),
            "parser": str(parser_script),
            "image": f"jefzda/sweap-images:{args.dockerhub_tag}",
        }
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        with args.trial_log.open("a", encoding="utf-8") as log:
            log.write(f"{status}: {detail}\n")
        print(f"Pro verifier reward: {reward}")
        return exit_code

    if not args.preds.is_file() or not args.preds.stat().st_size:
        return finish(0, "PRO_OUTPUT_MISSING", f"{args.preds} missing or empty", exit_code=2)
    if not eval_script.is_file():
        return finish(0, "PRO_EVAL_SCRIPT_MISSING", f"{eval_script} absent", exit_code=2)
    if not run_script.is_file():
        return finish(0, "PRO_RUN_SCRIPT_MISSING", f"{run_script} absent", exit_code=2)
    if not parser_script.is_file():
        return finish(0, "PRO_PARSER_MISSING", f"{parser_script} absent", exit_code=2)

    patch_text = find_patch_text(args.preds, args.instance_id)
    if not patch_text.strip():
        return finish(0, "PRO_EVAL_SKIP", "no agent patch found")

    try:
        patch_path = write_patch_json(args.output_dir, args.instance_id, patch_text)
        raw_sample_path = write_raw_sample_csv(args.output_dir, args.instance_id)
    except Exception as exc:
        return finish(0, "PRO_EVAL_INPUT_FAIL", repr(exc), exit_code=2)

    cmd = [
        sys.executable,
        str(eval_script.resolve()),
        "--raw_sample_path",
        str(raw_sample_path.resolve()),
        "--patch_path",
        str(patch_path.resolve()),
        "--output_dir",
        str(args.output_dir.resolve()),
        "--scripts_dir",
        str(scripts_dir.resolve()),
        "--dockerhub_username",
        "jefzda",
        "--use_local_docker",
        "--num_workers",
        str(args.num_workers),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(args.pro_os_dir.resolve()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = proc.stdout or ""
    except FileNotFoundError as exc:
        return finish(0, "PRO_EVAL_FAIL", f"official evaluator failed to start: {exc}", exit_code=2)

    eval_log = args.output_dir / "pro_eval_output.log"
    eval_log.write_text(output, encoding="utf-8", errors="replace")
    with args.trial_log.open("a", encoding="utf-8") as log:
        tail = "\n".join(output.splitlines()[-80:])
        if tail:
            log.write(tail + "\n")

    result_path = args.output_dir / "eval_results.json"
    reward = 0
    result_missing = False
    if result_path.is_file():
        try:
            results = load_json(result_path)
            reward = 1 if bool(results.get(args.instance_id)) else 0
        except Exception:
            reward = 0
    else:
        result_missing = True
    if result_missing and proc.returncode != 0:
        return finish(
            0,
            "PRO_EVAL_INFRA_FAIL",
            "official swe_bench_pro_eval.py failed before eval_results.json",
            proc.returncode,
            exit_code=2,
        )
    status = "PRO_EVAL_PASS" if reward else "PRO_EVAL_FAIL"
    detail = "official swe_bench_pro_eval.py result"
    return finish(reward, status, detail, proc.returncode)


if __name__ == "__main__":
    sys.exit(main())
