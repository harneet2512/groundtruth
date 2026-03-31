"""SWE-bench benchmark configuration."""

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AgentMode(Enum):
    BASELINE = "baseline"
    GROUNDTRUTH = "groundtruth"
    GROUNDTRUTH_MCP = "groundtruth_mcp"  # real MCP server, proof required
    GROUNDTRUTH_V2 = "groundtruth_v2"  # passive: context injection + post-edit validation
    GROUNDTRUTH_V2_PULL = "groundtruth_v2_pull"  # pull: 3 tools + lifecycle hooks


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass
class SWEBenchConfig:
    """Configuration for a SWE-bench benchmark run."""

    # Model (use MODEL_NAME_EXACT env for resolved GPT-5 mini or other)
    model: str = "gpt-5-mini"
    max_tokens_per_turn: int = 4096
    temperature: float = 0.0

    # Agent
    mode: AgentMode = AgentMode.BASELINE
    max_turns: int = 30
    max_cost_per_task: float = 0.50  # USD, safety cap

    # Dataset
    dataset: str = "princeton-nlp/SWE-bench_Lite"
    split: str = "test"
    instance_ids: list[str] = field(default_factory=list)  # empty = all

    # Execution
    workers: int = 1
    timeout_seconds: int = 600  # per task
    clean_after: bool = True  # remove Docker containers after eval

    # Sharding / worker identity (from env: TASK_SHARD_INDEX, TASK_SHARD_TOTAL, WORKER_ID)
    shard_index: int = 0
    shard_total: int = 1
    worker_id: int = 0

    # Condition label (from env BENCHMARK_CONDITION: baseline_no_mcp | with_groundtruth_mcp)
    condition: str = "baseline_no_mcp"

    # Paths
    output_dir: Path = Path("benchmarks/swebench/results")
    predictions_file: str = "predictions.jsonl"

    # GroundTruth indexing
    gt_index_timeout: int = 120  # seconds to index repo
    gt_db_path: str = ""  # empty = auto (file-based for MCP, :memory: for direct)

    # Resume support
    resume: bool = True  # skip already-completed tasks on restart

    # Trace output
    save_traces: bool = False  # write per-task prediction to trajs/ dir

    # MCP server (for GROUNDTRUTH_MCP mode)
    mcp_no_auto_index: bool = False  # if True, server expects pre-built db

    def __post_init__(self) -> None:
        if self.shard_total == 1 and self.worker_id == 0:
            self.shard_index = _env_int("TASK_SHARD_INDEX", 0)
            self.shard_total = _env_int("TASK_SHARD_TOTAL", 1)
            self.worker_id = _env_int("WORKER_ID", 0)
        self.condition = _env_str("BENCHMARK_CONDITION", self.condition)
        model_env = _env_str("MODEL_NAME_EXACT", "")
        if model_env:
            self.model = model_env

    @property
    def run_id(self) -> str:
        mode_str = self.mode.value
        model_short = self.model.replace("-", "")
        return f"groundtruth-{mode_str}-{model_short}"

    @property
    def predictions_path(self) -> Path:
        return self.output_dir / self.mode.value / self.predictions_file
