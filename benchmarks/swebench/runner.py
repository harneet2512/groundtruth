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
from .mcp_bridge import MCPBridge

logger = logging.getLogger(__name__)

# Cache for pre-built indexes by repo key (repo_name + base_commit)
_index_cache: dict[str, str] = {}  # repo_key -> db_path


def _truncate_messages(messages: list, max_chars: int = 2000) -> list:
    """Truncate tool results for manageable trace files."""
    result = []
    for msg in messages:
        msg = dict(msg) if isinstance(msg, dict) else msg
        if isinstance(msg, dict) and msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_chars:
                msg = dict(msg)
                msg["content"] = content[:max_chars] + f"\n... [truncated {len(content) - max_chars} chars]"
        result.append(msg)
    return result


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
    proof_output_dir: Path | None = None,
) -> dict:
    """Run a single SWE-bench task. Returns a prediction dict."""
    instance_id = task["instance_id"]
    logger.info("Starting task: %s", instance_id)

    try:
        # Setup repo
        repo_path = setup_repo(task, work_dir)

        # Initialize bridge based on mode
        gt_bridge = None
        gt_integration = None
        gt_v2_bridge = None
        gt_v2_hooks = None

        if config.mode == AgentMode.GROUNDTRUTH_V2_PULL:
            gt_v2_bridge, gt_v2_hooks = _init_gt_v2_pull(repo_path, instance_id, config)
        elif config.mode == AgentMode.GROUNDTRUTH_V2:
            gt_integration = _init_gt_v2(repo_path, instance_id, config)
        elif config.mode == AgentMode.GROUNDTRUTH:
            gt_bridge = GroundTruthBridge(
                repo_path=repo_path,
                db_path=config.gt_db_path or ":memory:",
                index_timeout=config.gt_index_timeout,
            )
            success = await gt_bridge.initialize()
            if not success:
                logger.warning("GroundTruth init failed for %s, running without GT", instance_id)
                gt_bridge = None
        elif config.mode == AgentMode.GROUNDTRUTH_MCP:
            gt_bridge = MCPBridge(
                repo_path=repo_path,
                db_path=config.gt_db_path or None,
                no_auto_index=config.mcp_no_auto_index,
                worker_id=config.worker_id,
                shard_id=config.shard_index,
                model_name_exact=config.model,
                proof_output_dir=str(proof_output_dir) if proof_output_dir else None,
                instance_id=instance_id,
            )
            success = await gt_bridge.connect()
            if not success:
                logger.warning("MCP connect failed for %s, running without GT", instance_id)
                gt_bridge = None

        # Run agent
        agent = SWEBenchAgent(
            config=config,
            cost_tracker=cost_tracker,
            repo_path=repo_path,
            gt_bridge=gt_bridge,
            gt_integration=gt_integration,
            gt_v2_bridge=gt_v2_bridge,
            gt_v2_hooks=gt_v2_hooks,
        )

        patch = await asyncio.wait_for(
            agent.solve(instance_id, task["problem_statement"]),
            timeout=config.timeout_seconds,
        )

        # Cleanup bridges
        if gt_bridge:
            await gt_bridge.shutdown()
        if gt_v2_bridge:
            gt_v2_bridge.shutdown()
        if gt_v2_hooks:
            gt_v2_hooks.shutdown()

        prediction = {
            "instance_id": instance_id,
            "model_name_or_path": config.run_id,
            "model_patch": patch or "",
            "turns_used": agent.turns_used,
        }

        # V2 pull: attach tool + hook logs
        if gt_v2_bridge is not None or gt_v2_hooks is not None:
            prediction["gt_v2_pull_report"] = {
                "tool_log": gt_v2_bridge.get_tool_log() if gt_v2_bridge else [],
                "hook_summary": gt_v2_hooks.get_summary() if gt_v2_hooks else {},
            }

        # V2 passive: attach GT report with context utilization
        if gt_integration is not None:
            from .gt_integration import GTIntegration

            gt: GTIntegration = gt_integration  # type: ignore[assignment]
            prediction["gt_report"] = gt.final_report()
            prediction["gt_report"]["context_utilization"] = gt.compute_context_utilization(patch)

        # Attach conversation for trace saving (stripped before JSONL write)
        prediction["_conversation"] = agent.conversation_history

        status = "patched" if patch else "no_patch"
        cost = cost_tracker.get_task_cost(instance_id)
        logger.info("Completed %s: %s ($%.4f, %d turns)", instance_id, status, cost, agent.turns_used)

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

    proof_output_dir = output_dir / "proof" if config.mode == AgentMode.GROUNDTRUTH_MCP else None
    if proof_output_dir is not None:
        proof_output_dir.mkdir(parents=True, exist_ok=True)

    # Load tasks
    tasks = load_tasks(config)
    cost_tracker = CostTracker(model=config.model)

    # Resume support: skip already-completed tasks
    completed_ids: set[str] = set()
    predictions: list[dict] = []
    if config.resume and predictions_path.exists():
        existing = predictions_path.read_text(encoding="utf-8").strip()
        if existing:
            for line in existing.splitlines():
                try:
                    pred = json.loads(line)
                    completed_ids.add(pred["instance_id"])
                    predictions.append(pred)
                except (json.JSONDecodeError, KeyError):
                    pass
            logger.info("Resuming: %d tasks already completed", len(completed_ids))
            tasks = [t for t in tasks if t["instance_id"] not in completed_ids]
            logger.info("Remaining: %d tasks", len(tasks))
    else:
        predictions_path.write_text("", encoding="utf-8")

    logger.info(
        "Running SWE-bench: mode=%s, model=%s, tasks=%d, workers=%d",
        config.mode.value, config.model, len(tasks), config.workers,
    )

    # Create work directory for repos
    work_dir = tempfile.mkdtemp(prefix="swebench_")
    predictions_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(config.workers)

    dashboard = ProgressDashboard(total=len(tasks))

    async def run_guarded(task: dict) -> dict:
        async with semaphore:
            prediction = await run_single_task(
                task, config, cost_tracker, work_dir,
                proof_output_dir=proof_output_dir,
            )
            async with predictions_lock:
                predictions.append(prediction)
                # Strip conversation from JSONL (it's saved in trajs/)
                jsonl_pred = {k: v for k, v in prediction.items() if k != "_conversation"}
                with open(predictions_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(jsonl_pred, default=str) + "\n")
                dashboard.record(prediction)
                logger.info(dashboard.summary())
                # Save per-task trace with full conversation history
                if config.save_traces:
                    trajs_dir = output_dir / "trajs"
                    trajs_dir.mkdir(parents=True, exist_ok=True)
                    trace_data = dict(prediction)
                    trace_data["conversation"] = _truncate_messages(trace_data.pop("_conversation", []))
                    trace_path = trajs_dir / f"{prediction['instance_id']}.json"
                    trace_path.write_text(
                        json.dumps(trace_data, indent=2, default=str),
                        encoding="utf-8",
                    )
            return prediction

    await asyncio.gather(*[run_guarded(t) for t in tasks])

    # Save cost report
    cost_path = output_dir / "cost_report.json"
    cost_tracker.save(cost_path)

    # Write run metadata
    write_metadata(config, output_dir, predictions)

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
    parser.add_argument(
        "--mode",
        choices=["baseline", "groundtruth", "groundtruth_mcp", "groundtruth_v2", "groundtruth_v2_pull"],
        default="baseline",
    )
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-cost-per-task", type=float, default=0.50)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--instance-ids", nargs="*", default=[])
    parser.add_argument("--output-dir", default="benchmarks/swebench/results")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    parser.add_argument("--gt-index-timeout", type=int, default=120)
    parser.add_argument("--resume", action="store_true", default=True, help="Resume from previous run (default)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Start fresh, overwrite previous results")
    parser.add_argument("--split", default="test", help="Dataset split (default: test)")
    parser.add_argument("--save-traces", action="store_true", help="Save per-task prediction to trajs/ dir")
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
        instance_ids=args.instance_ids if args.instance_ids else [],
        output_dir=Path(args.output_dir),
        dataset=args.dataset,
        gt_index_timeout=args.gt_index_timeout,
        resume=args.resume,
        split=args.split,
        save_traces=args.save_traces,
    )

    asyncio.run(run_benchmark(config))


def _init_gt_v2_pull(
    repo_path: str,
    instance_id: str,
    config: SWEBenchConfig,
) -> tuple:
    """Initialize v2 pull architecture: bridge + hooks backed by graph.db.

    Returns (GTV2Bridge | None, GTV2Hooks | None).
    """
    import glob as _glob
    import os as _os
    import subprocess as _subprocess

    from .gt_v2_bridge import GTV2Bridge
    from .gt_v2_hooks import GTV2Hooks

    # Build graph.db using the Go indexer (gt-index)
    db_path = config.gt_db_path or ""
    if not db_path:
        db_path = _os.path.join(repo_path, ".gt_graph.db")

    # Try to build the index if it doesn't exist
    if not _os.path.exists(db_path):
        # Check GT_INDEX_BIN env var first, then PATH
        gt_index_bin = _os.environ.get("GT_INDEX_BIN") or shutil.which("gt-index")
        if gt_index_bin and _os.path.exists(gt_index_bin):
            try:
                _subprocess.run(
                    [gt_index_bin, "-root", repo_path, "-output", db_path],
                    capture_output=True,
                    timeout=config.gt_index_timeout,
                )
            except Exception:
                logger.warning("gt-index failed for %s", instance_id)

    if not _os.path.exists(db_path):
        logger.warning("No graph.db for %s — v2 pull running without GT", instance_id)
        return None, None

    # Set up log directory
    log_dir = str(config.output_dir / config.mode.value / "gt_logs")

    # Initialize bridge
    bridge = GTV2Bridge(db_path=db_path, repo_path=repo_path, log_dir=log_dir)
    bridge.set_task_id(instance_id)
    if not bridge.connect():
        logger.warning("v2 pull bridge connect failed for %s", instance_id)
        return None, None

    # Initialize hooks
    hooks = GTV2Hooks(db_path=db_path, repo_path=repo_path, log_dir=log_dir)
    hooks.set_task_id(instance_id)
    if not hooks.connect():
        logger.warning("v2 pull hooks connect failed for %s", instance_id)
        bridge.shutdown()
        return None, None

    logger.info("GT v2 pull initialized for %s (db=%s)", instance_id, db_path)
    return bridge, hooks


def _init_gt_v2(
    repo_path: str,
    instance_id: str,
    config: SWEBenchConfig,
) -> object | None:
    """Initialize passive GT integration for V2 mode. Returns GTIntegration or None."""
    import glob
    import time as _time

    from groundtruth.index.ast_parser import parse_python_file
    from groundtruth.index.store import SymbolStore
    from .gt_integration import GTIntegration, GT_ARTIFACT_VERSION

    try:
        store = SymbolStore(":memory:")
        store.initialize()
        store.set_metadata("artifact_version", GT_ARTIFACT_VERSION)

        gt = GTIntegration(store=store, repo_path=repo_path)

        # Index all Python files
        start = _time.monotonic()
        py_files = glob.glob(f"{repo_path}/**/*.py", recursive=True)
        symbol_count = 0
        for fpath in py_files:
            # Skip very large files (>500KB) and hidden dirs
            try:
                if "/.git/" in fpath or "\\.git\\" in fpath:
                    continue
                symbols = parse_python_file(fpath)
                now = int(_time.time())
                for sym in symbols:
                    store.insert_symbol(
                        name=sym.name,
                        kind=sym.kind,
                        language="python",
                        file_path=fpath,
                        line_number=sym.line,
                        end_line=sym.end_line,
                        is_exported=sym.is_exported,
                        signature=sym.signature,
                        params=None,
                        return_type=sym.return_type,
                        documentation=sym.documentation,
                        last_indexed_at=now,
                    )
                    symbol_count += 1
                    for child in sym.children:
                        store.insert_symbol(
                            name=child.name,
                            kind=child.kind,
                            language="python",
                            file_path=fpath,
                            line_number=child.line,
                            end_line=child.end_line,
                            is_exported=child.is_exported,
                            signature=child.signature,
                            params=None,
                            return_type=child.return_type,
                            documentation=child.documentation,
                            last_indexed_at=now,
                        )
                        symbol_count += 1

                # Timeout guard
                if (_time.monotonic() - start) > config.gt_index_timeout:
                    logger.warning("GT index timeout for %s after %d files", instance_id, len(py_files))
                    break
            except Exception:
                continue

        elapsed = _time.monotonic() - start
        gt.mark_index_complete(elapsed, symbol_count)
        logger.info(
            "GT V2 indexed %s: %d symbols in %.1fs",
            instance_id, symbol_count, elapsed,
        )
        return gt

    except Exception:
        logger.exception("GT V2 init failed for %s", instance_id)
        return None


class ProgressDashboard:
    """Simple progress tracker for benchmark runs."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.completed = 0
        self.patched = 0
        self.failed = 0
        self._start = __import__("time").monotonic()

    def record(self, prediction: dict) -> None:
        self.completed += 1
        if prediction.get("model_patch", "").strip():
            self.patched += 1
        else:
            self.failed += 1

    def summary(self) -> str:
        elapsed = __import__("time").monotonic() - self._start
        rate = self.completed / max(elapsed, 1) * 60  # tasks/min
        return (
            f"[{self.completed}/{self.total}] "
            f"patched={self.patched} no_patch={self.failed} "
            f"({rate:.1f} tasks/min)"
        )


def write_metadata(config: SWEBenchConfig, output_dir: Path, predictions: list[dict]) -> None:
    """Write run metadata YAML to the output directory."""
    import time as _time

    meta = {
        "run_id": config.run_id,
        "mode": config.mode.value,
        "model": config.model,
        "dataset": config.dataset,
        "total_tasks": len(predictions),
        "patched": sum(1 for p in predictions if p.get("model_patch", "").strip()),
        "max_turns": config.max_turns,
        "max_cost_per_task": config.max_cost_per_task,
        "timeout_seconds": config.timeout_seconds,
        "workers": config.workers,
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Turn count stats
    turns = [p.get("turns_used", 0) for p in predictions if p.get("turns_used")]
    if turns:
        meta["turn_stats"] = {
            "avg_turns": round(sum(turns) / len(turns), 1),
            "min_turns": min(turns),
            "max_turns": max(turns),
        }

    # Include V2 GT stats if available
    gt_reports = [p.get("gt_report") for p in predictions if p.get("gt_report")]
    if gt_reports:
        total_validations = sum(
            int(r.get("instrumentation", {}).get("validations_fired", 0))
            for r in gt_reports
        )
        total_fixes = sum(
            int(r.get("instrumentation", {}).get("agent_fixed_after_validation", 0))
            for r in gt_reports
        )
        total_fp_reexport = sum(
            int(r.get("instrumentation", {}).get("validations_likely_fp_reexport", 0))
            for r in gt_reports
        )

        # Context utilization aggregate
        util_rates = [
            r.get("context_utilization", {}).get("utilization_rate", 0.0)
            for r in gt_reports
            if r.get("context_utilization")
        ]
        avg_util = round(sum(util_rates) / len(util_rates), 3) if util_rates else 0.0

        # Validation latency aggregate
        all_latencies: list[int] = []
        for r in gt_reports:
            lats = r.get("instrumentation", {}).get("validation_latency_ms", [])
            if isinstance(lats, list):
                all_latencies.extend(lats)
        avg_latency = round(sum(all_latencies) / len(all_latencies)) if all_latencies else 0

        meta["gt_v2_stats"] = {
            "tasks_with_gt": len(gt_reports),
            "total_validations_fired": total_validations,
            "total_agent_fixes_after_validation": total_fixes,
            "total_fp_reexport_suppressed": total_fp_reexport,
            "avg_context_utilization": avg_util,
            "avg_validation_latency_ms": avg_latency,
        }

    meta_path = output_dir / "run_metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info("Metadata written to %s", meta_path)


if __name__ == "__main__":
    main()
