"""Track OpenAI API costs per task and across the run."""

from dataclasses import dataclass, field
import json
from pathlib import Path


# Model pricing (per million tokens)
MODEL_COSTS = {
    "gpt-5-mini": {"input": 0.15, "output": 0.60},
}


@dataclass
class TaskCost:
    instance_id: str
    model: str = "gpt-5-mini"
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0

    @property
    def input_cost(self) -> float:
        costs = MODEL_COSTS.get(self.model, {"input": 0.15, "output": 0.60})
        return self.input_tokens * costs["input"] / 1_000_000

    @property
    def output_cost(self) -> float:
        costs = MODEL_COSTS.get(self.model, {"input": 0.15, "output": 0.60})
        return self.output_tokens * costs["output"] / 1_000_000

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.output_cost

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "turns": self.turns,
            "input_cost": round(self.input_cost, 6),
            "output_cost": round(self.output_cost, 6),
            "total_cost": round(self.total_cost, 6),
        }


@dataclass
class CostTracker:
    model: str = "gpt-5-mini"
    tasks: dict[str, TaskCost] = field(default_factory=dict)

    def record(self, instance_id: str, input_tokens: int, output_tokens: int) -> None:
        if instance_id not in self.tasks:
            self.tasks[instance_id] = TaskCost(instance_id=instance_id, model=self.model)
        task = self.tasks[instance_id]
        task.input_tokens += input_tokens
        task.output_tokens += output_tokens
        task.turns += 1

    def get_task_cost(self, instance_id: str) -> float:
        if instance_id in self.tasks:
            return self.tasks[instance_id].total_cost
        return 0.0

    @property
    def total_cost(self) -> float:
        return sum(t.total_cost for t in self.tasks.values())

    @property
    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.tasks.values())

    @property
    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.tasks.values())

    def summary(self) -> dict:
        return {
            "model": self.model,
            "total_tasks": len(self.tasks),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": round(self.total_cost, 4),
            "avg_cost_per_task": round(self.total_cost / max(len(self.tasks), 1), 6),
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.summary(), indent=2))
