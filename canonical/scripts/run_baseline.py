#!/usr/bin/env python3
"""Canonical SWE-agent + DeepSeek V3.2 baseline runner — NO GroundTruth.

Pure baseline for A/B comparison. Uses real SWE-agent with DeepSeek V3.2.
No GT injection, no GT tools, no GT state command.

Usage:
    python canonical/scripts/run_baseline.py \\
        --config canonical/config/sweagent_deepseek_v3.2_baseline.yaml \\
        --output-dir results/canonical_baseline \\
        --workers 4 \\
        [--instance-ids "repo__owner-123,repo__owner-456"]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess as sp
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("canonical_baseline")


def _inject_live_docker_image(instance: dict) -> dict:
    """Inject docker_image for SWE-bench Live instances."""
    if "docker_image" not in instance and "dockerhub_tag" not in instance:
        iid = instance["instance_id"]
        id_docker = iid.replace("__", "_1776_")
        instance["docker_image"] = f"starryzhang/sweb.eval.x86_64.{id_docker}:latest"
    return instance


def load_dataset(dataset: str, split: str, instance_ids: list[str] | None,
                 max_instances: int | None) -> list[dict]:  # type: ignore[type-arg]
    """Load SWE-bench-Live Lite dataset."""
    from datasets import load_dataset as hf_load
    ds = hf_load(dataset, split=split)
    instances: list[dict] = [dict(row) for row in ds]  # type: ignore[arg-type]

    if instance_ids:
        id_set = set(instance_ids)
        instances = [i for i in instances if i["instance_id"] in id_set]
    if max_instances:
        instances = instances[:max_instances]
    return instances


def process_instance(
    instance: dict, config_path: str, output_dir: Path,
) -> dict:
    """Process a single instance — pure SWE-agent, no GT."""
    instance = _inject_live_docker_image(instance)
    instance_id = instance["instance_id"]
    t0 = time.time()

    instance_dir = output_dir / "trajs" / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "instance_id": instance_id,
        "patch": "",
        "exit_status": "unknown",
        "runtime_s": 0,
        "error": None,
    }

    container_id = None
    try:
        docker_image = instance.get("docker_image", "")
        container_id = sp.run(
            ["docker", "run", "-d", "--rm", "--entrypoint", "",
             "-w", "/testbed", docker_image, "sleep", "7200"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout.strip()

        logger.info("Container: %s | %s", instance_id, container_id[:12])

        # Write instance for SWE-agent
        instance_file = instance_dir / "instance.json"
        instance_file.write_text(json.dumps(instance, default=str))

        sweagent_cmd = [
            sys.executable, "-m", "sweagent", "run",
            "--config", config_path,
            "--instance", str(instance_file),
            "--output-dir", str(instance_dir),
        ]

        proc = sp.run(sweagent_cmd, capture_output=True, text=True, timeout=1800,
                       env={**os.environ, "DOCKER_CONTAINER_ID": container_id})

        result["exit_status"] = "completed" if proc.returncode == 0 else f"error_{proc.returncode}"
        if proc.returncode != 0:
            result["error"] = proc.stderr[-500:] if proc.stderr else None

        # Extract patch
        try:
            patch = sp.run(
                ["docker", "exec", container_id, "bash", "-c",
                 "cat /testbed/patch.txt 2>/dev/null || cd /testbed && git diff"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            result["patch"] = patch
        except Exception:
            pass

    except sp.TimeoutExpired:
        result["exit_status"] = "timeout"
    except Exception as e:
        result["exit_status"] = "infra_error"
        result["error"] = str(e)[:300]
    finally:
        if container_id:
            try:
                sp.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=10)
            except Exception:
                pass
        result["runtime_s"] = round(time.time() - t0, 1)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-agent + DeepSeek V3.2 baseline (no GT)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dataset", default="SWE-bench-Live/SWE-bench-Live")
    parser.add_argument("--split", default="lite")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--instance-ids", default="")
    parser.add_argument("--max-instances", type=int, default=None)
    parser.add_argument("--model-name", default="sweagent+deepseek-v3.2-baseline")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "submission").mkdir(exist_ok=True)
    (output_dir / "trajs").mkdir(exist_ok=True)

    shutil.copy2(Path(args.config).resolve(), output_dir / "run_config.yaml")

    ids = [i.strip() for i in args.instance_ids.split(",") if i.strip()] if args.instance_ids else None
    instances = load_dataset(args.dataset, args.split, ids, args.max_instances)
    logger.info("Loaded %d instances", len(instances))

    preds_path = output_dir / "submission" / "all_preds.jsonl"
    results: list[dict] = []
    completed = 0

    for inst in instances:
        r = process_instance(inst, str(Path(args.config).resolve()), output_dir)
        results.append(r)
        completed += 1

        if r["patch"]:
            entry = {"instance_id": r["instance_id"], "model_patch": r["patch"],
                     "model_name_or_path": args.model_name}
            with open(preds_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

        logger.info("[%d/%d] %s | %s | patch=%s | %.1fs",
                    completed, len(instances), r["instance_id"],
                    r["exit_status"], "yes" if r["patch"] else "EMPTY", r["runtime_s"])

    # Summary
    summary = {
        "total": len(instances), "completed": completed,
        "patches": sum(1 for r in results if r["patch"]),
        "errors": sum(1 for r in results if r["exit_status"] not in ("completed",)),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    logger.info("Done: %s", summary)


if __name__ == "__main__":
    main()
