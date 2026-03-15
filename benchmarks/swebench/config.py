"""SWE-bench benchmark configuration."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AgentMode(Enum):
    BASELINE = "baseline"
    GROUNDTRUTH = "groundtruth"


@dataclass
class SWEBenchConfig:
    """Configuration for a SWE-bench benchmark run."""

    # Model
    model: str = "gpt-4o-mini"
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

    # Paths
    output_dir: Path = Path("benchmarks/swebench/results")
    predictions_file: str = "predictions.jsonl"

    # GroundTruth indexing
    gt_index_timeout: int = 120  # seconds to index repo
    gt_db_path: str = ":memory:"  # in-memory SQLite for speed

    @property
    def run_id(self) -> str:
        mode_str = self.mode.value
        model_short = self.model.replace("-", "")
        return f"groundtruth-{mode_str}-{model_short}"

    @property
    def predictions_path(self) -> Path:
        return self.output_dir / self.mode.value / self.predictions_file
