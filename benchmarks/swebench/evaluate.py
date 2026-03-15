"""Evaluate SWE-bench predictions using the official Docker evaluator."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def evaluate_predictions(
    predictions_path: Path,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
    run_id: str = "groundtruth",
    max_workers: int = 4,
    timeout: int = 1800,
) -> dict:
    """
    Run SWE-bench Docker evaluation on predictions.

    Uses `swebench.harness.run_evaluation` or the `sb-cli` tool.
    Returns evaluation results dict.
    """
    results_dir = predictions_path.parent / "eval_results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # Try sb-cli first (cleaner interface)
    try:
        result = _eval_with_python_api(predictions_path, dataset, split, run_id, max_workers, timeout)
        if result:
            # Save results
            results_file = results_dir / "evaluation.json"
            results_file.write_text(json.dumps(result, indent=2))
            return result
    except Exception:
        logger.warning("Python API evaluation failed, trying CLI fallback")

    # Fallback: use swebench CLI
    try:
        result = _eval_with_cli(predictions_path, dataset, run_id, max_workers, timeout)
        results_file = results_dir / "evaluation.json"
        results_file.write_text(json.dumps(result, indent=2))
        return result
    except Exception:
        logger.exception("All evaluation methods failed")
        return {"error": "Evaluation failed"}


def _eval_with_python_api(
    predictions_path: Path,
    dataset: str,
    split: str,
    run_id: str,
    max_workers: int,
    timeout: int,
) -> dict | None:
    """Evaluate using SWE-bench Python API."""
    try:
        from swebench.harness.run_evaluation import main as run_eval

        run_eval(
            dataset_name=dataset,
            split=split,
            predictions_path=str(predictions_path),
            run_id=run_id,
            max_workers=max_workers,
            timeout=timeout,
        )

        # Load results — swebench writes to a standard location
        # Check common output patterns
        for candidate in [
            predictions_path.parent / "eval_results" / f"{run_id}.json",
            Path(f"results/{run_id}/{dataset.split('/')[-1]}.json"),
            predictions_path.parent / f"{run_id}_results.json",
        ]:
            if candidate.exists():
                return json.loads(candidate.read_text())

        logger.warning("Evaluation ran but results file not found")
        return None
    except ImportError:
        logger.warning("swebench.harness.run_evaluation not available")
        return None


def _eval_with_cli(
    predictions_path: Path,
    dataset: str,
    run_id: str,
    max_workers: int,
    timeout: int,
) -> dict:
    """Evaluate using swebench CLI."""
    cmd = [
        "python", "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset,
        "--predictions_path", str(predictions_path),
        "--run_id", run_id,
        "--max_workers", str(max_workers),
        "--timeout", str(timeout),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 300,  # Extra buffer
    )

    if result.returncode != 0:
        logger.error("swebench CLI failed: %s", result.stderr)
        raise RuntimeError(f"swebench evaluation failed: {result.stderr}")

    # Try to find results
    return {"status": "completed", "output": result.stdout[-2000:]}


def compute_pass_rate(results: dict) -> dict:
    """Compute pass@1 rate from evaluation results."""
    if "error" in results:
        return results

    # SWE-bench results format varies — handle common shapes
    resolved = 0
    total = 0

    if isinstance(results, dict):
        for instance_id, result in results.items():
            if instance_id.startswith("__"):
                continue
            total += 1
            if isinstance(result, dict) and result.get("resolved", False):
                resolved += 1
            elif isinstance(result, bool) and result:
                resolved += 1

    if total == 0:
        return {"error": "No evaluation results found", "raw": results}

    return {
        "resolved": resolved,
        "total": total,
        "pass_rate": round(resolved / total * 100, 2),
    }


def main() -> None:
    """CLI entry point for evaluation."""
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate SWE-bench predictions")
    parser.add_argument("predictions", type=Path, help="Path to predictions.jsonl")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    parser.add_argument("--run-id", default="groundtruth")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    results = evaluate_predictions(
        predictions_path=args.predictions,
        dataset=args.dataset,
        run_id=args.run_id,
        max_workers=args.max_workers,
        timeout=args.timeout,
    )

    pass_rate = compute_pass_rate(results)
    print(json.dumps(pass_rate, indent=2))


if __name__ == "__main__":
    main()
