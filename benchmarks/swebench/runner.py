"""SWE-bench runner — orchestrates task execution and prediction generation."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from datasets import load_dataset

from .config import SWEBenchConfig, AgentMode
from .cost_tracker import CostTracker
from .agent import SWEBenchAgent
from .groundtruth_bridge import GroundTruthBridge

logger = logging.getLogger(__name__)


def load_tasks(config: SWEBenchConfig) -> list[dict]:
    """Load SWE-bench tasks from HuggingFace."""
    ds = load_dataset(config.dataset, split=config.split)
    tasks = list(ds)

    if config.instance_ids:
        tasks = [t for t in tasks if t["instance_id"] in config.instance_ids]
        logger.info("Filtered to %d tasks by instance_id", len(tasks))

    logger.info("Loaded %d tasks from %s", len(tasks), config.dataset)
    return tasks


def setup_repo(task: dict, work_dir: str) -> str:
    """Clone and checkout the repo at the correct commit for a task.

    Returns the path to the repo directory.
    """
    repo = task["repo"]
    base_commit = task["base_commit"]
    repo_dir = Path(work_dir) / repo.replace("/", "__")
    git_bin = shutil.which("git") or "git"

    if repo_dir.exists():
        # Reset to correct commit
        subprocess.run(
            [git_bin, "checkout", "-f", base_commit],
            cwd=str(repo_dir),
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            [git_bin, "clean", "-fdx"],
            cwd=str(repo_dir),
            capture_output=True,
            timeout=60,
        )
    else:
        # Clone
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [git_bin, "clone", f"https://github.com/{repo}.git", str(repo_dir)],
            capture_output=True,
            timeout=300,
        )
        subprocess.run(
            [git_bin, "checkout", "-f", base_commit],
            cwd=str(repo_dir),
            capture_output=True,
            timeout=60,
        )

    return str(repo_dir)


async def run_single_task(
    task: dict,
    config: SWEBenchConfig,
    cost_tracker: CostTracker,
    work_dir: str,
) -> dict:
    """Run a single SWE-bench task. Returns a prediction dict."""
    instance_id = task["instance_id"]
    logger.info("Starting task: %s", instance_id)

    try:
        # Setup repo
        repo_path = setup_repo(task, work_dir)

        # Initialize GroundTruth bridge if needed
        gt_bridge = None
        if config.mode == AgentMode.GROUNDTRUTH:
            gt_bridge = GroundTruthBridge(
                repo_path=repo_path,
                db_path=config.gt_db_path,
                index_timeout=config.gt_index_timeout,
            )
            success = await gt_bridge.initialize()
            if not success:
                logger.warning("GroundTruth init failed for %s, running without GT", instance_id)
                gt_bridge = None

        # Run agent
        agent = SWEBenchAgent(
            config=config,
            cost_tracker=cost_tracker,
            repo_path=repo_path,
            gt_bridge=gt_bridge,
        )

        patch = await asyncio.wait_for(
            agent.solve(instance_id, task["problem_statement"]),
            timeout=config.timeout_seconds,
        )

        # Cleanup bridge
        if gt_bridge:
            await gt_bridge.shutdown()

        prediction = {
            "instance_id": instance_id,
            "model_name_or_path": config.run_id,
            "model_patch": patch or "",
        }

        status = "patched" if patch else "no_patch"
        cost = cost_tracker.get_task_cost(instance_id)
        logger.info("Completed %s: %s ($%.4f)", instance_id, status, cost)

        return prediction

    except asyncio.TimeoutError:
        logger.error("Task %s timed out after %ds", instance_id, config.timeout_seconds)
        return {
            "instance_id": instance_id,
            "model_name_or_path": config.run_id,
            "model_patch": "",
        }
    except Exception:
        logger.exception("Task %s failed", instance_id)
        return {
            "instance_id": instance_id,
            "model_name_or_path": config.run_id,
            "model_patch": "",
        }


async def run_benchmark(config: SWEBenchConfig) -> Path:
    """Run the full SWE-bench benchmark. Returns path to predictions file."""
    # Setup output directory
    output_dir = config.output_dir / config.mode.value
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = output_dir / config.predictions_file

    # Load tasks
    tasks = load_tasks(config)
    cost_tracker = CostTracker(model=config.model)

    logger.info(
        "Running SWE-bench: mode=%s, model=%s, tasks=%d, workers=%d",
        config.mode.value, config.model, len(tasks), config.workers,
    )

    # Create work directory for repos
    work_dir = tempfile.mkdtemp(prefix="swebench_")

    # Run tasks (sequential for now — parallel adds complexity with Docker)
    predictions = []
    for i, task in enumerate(tasks):
        logger.info("Progress: %d/%d", i + 1, len(tasks))
        prediction = await run_single_task(task, config, cost_tracker, work_dir)
        predictions.append(prediction)

        # Append to predictions file incrementally
        with open(predictions_path, "a") as f:
            f.write(json.dumps(prediction) + "\n")

    # Save cost report
    cost_path = output_dir / "cost_report.json"
    cost_tracker.save(cost_path)

    logger.info(
        "Benchmark complete: %d predictions, total cost $%.4f",
        len(predictions), cost_tracker.total_cost,
    )
    logger.info("Predictions: %s", predictions_path)
    logger.info("Cost report: %s", cost_path)

    return predictions_path


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Run SWE-bench benchmark with GroundTruth")
    parser.add_argument("--mode", choices=["baseline", "groundtruth"], default="baseline")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-cost-per-task", type=float, default=0.50)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--instance-ids", nargs="*", default=[])
    parser.add_argument("--output-dir", default="benchmarks/swebench/results")
    parser.add_argument("--gt-index-timeout", type=int, default=120)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = SWEBenchConfig(
        mode=AgentMode(args.mode),
        model=args.model,
        workers=args.workers,
        max_turns=args.max_turns,
        max_cost_per_task=args.max_cost_per_task,
        timeout_seconds=args.timeout,
        instance_ids=args.instance_ids,
        output_dir=Path(args.output_dir),
        gt_index_timeout=args.gt_index_timeout,
    )

    asyncio.run(run_benchmark(config))


if __name__ == "__main__":
    main()
