"""Evaluation Matrix Orchestrator — runs GT across benchmark families.

Orchestrates multi-benchmark, multi-model evaluation with repeated runs.
Produces structured results for comparison and acceptance checking.

Usage:
    python -m benchmarks.eval_matrix.run_matrix --config config.yaml
    python -m benchmarks.eval_matrix.run_matrix --benchmark static_fixing --model primary
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from importlib import import_module
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Result of a single benchmark run."""

    benchmark: str
    model: str
    run_id: int
    timestamp: int
    resolved: int
    total: int
    resolved_rate: float
    metrics: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class MatrixResult:
    """Complete evaluation matrix result."""

    config_path: str
    started_at: int
    completed_at: int = 0
    runs: list[RunResult] = field(default_factory=list)


def load_config(config_path: str) -> dict:
    """Load evaluation matrix configuration."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def run_single(
    benchmark_key: str,
    benchmark_config: dict,
    model_key: str,
    model_config: dict,
    run_id: int,
    gt_enabled: bool = True,
) -> RunResult:
    """Run a single benchmark/model/run combination.

    Dispatches to the benchmark-specific harness configured in config.yaml.
    Supported forms:
    - benchmark_config.runner = "pkg.module:function"
    - benchmark_config.command = ["python", "-m", "..."]
    """
    logger.info(
        "Running %s on %s (run %d, gt=%s)",
        benchmark_key, model_key, run_id, gt_enabled,
    )

    timestamp = int(time.time())
    total = benchmark_config.get("task_count", 0)

    try:
        if benchmark_config.get("runner"):
            result = _run_python_runner(
                benchmark_key, benchmark_config, model_key, model_config, run_id, gt_enabled
            )
        elif benchmark_config.get("command"):
            result = _run_command_runner(
                benchmark_key, benchmark_config, model_key, model_config, run_id, gt_enabled
            )
        else:
            return RunResult(
                benchmark=benchmark_key,
                model=model_key,
                run_id=run_id,
                timestamp=timestamp,
                resolved=0,
                total=total,
                resolved_rate=0.0,
                metrics={},
                errors=[f"No runner configured for benchmark '{benchmark_key}'"],
            )
    except Exception as exc:
        return RunResult(
            benchmark=benchmark_key,
            model=model_key,
            run_id=run_id,
            timestamp=timestamp,
            resolved=0,
            total=total,
            resolved_rate=0.0,
            metrics={},
            errors=[str(exc)],
        )

    return RunResult(
        benchmark=benchmark_key,
        model=model_key,
        run_id=run_id,
        timestamp=timestamp,
        resolved=result.get("resolved", 0),
        total=result.get("total", total),
        resolved_rate=result.get("resolved_rate", 0.0),
        metrics=result.get("metrics", {}),
        errors=result.get("errors", []),
    )


def _run_python_runner(
    benchmark_key: str,
    benchmark_config: dict,
    model_key: str,
    model_config: dict,
    run_id: int,
    gt_enabled: bool,
) -> dict:
    module_name, func_name = benchmark_config["runner"].split(":", 1)
    module = import_module(module_name)
    func = getattr(module, func_name)
    return func(
        benchmark_key=benchmark_key,
        benchmark_config=benchmark_config,
        model_key=model_key,
        model_config=model_config,
        run_id=run_id,
        gt_enabled=gt_enabled,
    )


def _run_command_runner(
    benchmark_key: str,
    benchmark_config: dict,
    model_key: str,
    model_config: dict,
    run_id: int,
    gt_enabled: bool,
) -> dict:
    command = [
        str(part)
        .replace("{benchmark}", benchmark_key)
        .replace("{model}", model_key)
        .replace("{run_id}", str(run_id))
        .replace("{gt_enabled}", "1" if gt_enabled else "0")
        for part in benchmark_config["command"]
    ]
    env = {
        "GT_EVAL_BENCHMARK": benchmark_key,
        "GT_EVAL_MODEL": model_key,
        "GT_EVAL_PROVIDER": str(model_config.get("provider", "")),
        "GT_EVAL_GT_ENABLED": "1" if gt_enabled else "0",
    }
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )
    if proc.returncode != 0:
        return {"errors": [proc.stderr.strip() or proc.stdout.strip() or "command failed"]}
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Command runner must emit a JSON object")
    return payload


def run_matrix(config_path: str, benchmark: str | None = None, model: str | None = None) -> MatrixResult:
    """Run the full evaluation matrix (or a subset).

    Args:
        config_path: Path to config.yaml.
        benchmark: Optional single benchmark to run.
        model: Optional single model to run.

    Returns MatrixResult with all runs.
    """
    config = load_config(config_path)
    result = MatrixResult(
        config_path=config_path,
        started_at=int(time.time()),
    )

    benchmarks = config.get("benchmarks", {})
    models = config.get("models", {})
    n_runs = config.get("acceptance_rules", {}).get("min_runs_per_config", 3)

    # Filter if specific benchmark/model requested
    if benchmark:
        benchmarks = {k: v for k, v in benchmarks.items() if k == benchmark}
    if model:
        models = {k: v for k, v in models.items() if k == model}

    for bench_key, bench_config in benchmarks.items():
        for model_key, model_config in models.items():
            if model_config.get("optional") and model is None:
                continue  # Skip optional models unless explicitly requested

            for run_id in range(1, n_runs + 1):
                run_result = run_single(
                    bench_key, bench_config, model_key, model_config, run_id
                )
                result.runs.append(run_result)

    result.completed_at = int(time.time())
    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="GT Evaluation Matrix")
    parser.add_argument("--config", default="benchmarks/eval_matrix/config.yaml")
    parser.add_argument("--benchmark", help="Run specific benchmark only")
    parser.add_argument("--model", help="Run specific model only")
    parser.add_argument("--output", default="eval_matrix_results.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    result = run_matrix(args.config, args.benchmark, args.model)

    # Save results
    output_path = Path(args.output)
    output_path.write_text(json.dumps(asdict(result), indent=2))
    logger.info("Results saved to %s", output_path)

    # Print summary
    print(f"\nMatrix complete: {len(result.runs)} runs")
    for run in result.runs:
        status = "OK" if not run.errors else "ERROR"
        print(f"  {run.benchmark}/{run.model} run{run.run_id}: {run.resolved_rate:.1%} [{status}]")


if __name__ == "__main__":
    main()
