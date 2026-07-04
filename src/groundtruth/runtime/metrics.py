"""Runtime metrics collection for GT signals.

Collects per-layer and aggregate metrics during a task run.
Used by both OH adapter and MCP product face.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeMetrics:
    """Accumulates metrics during a single task run."""
    task_id: str = ""
    start_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    per_layer_emitted: dict[str, int] = field(default_factory=dict)
    per_layer_suppressed: dict[str, int] = field(default_factory=dict)
    per_layer_chars: dict[str, int] = field(default_factory=dict)
    per_tool_calls: dict[str, int] = field(default_factory=dict)
    reactions_followed: int = 0
    reactions_ignored: int = 0
    total_gt_chars_delivered: int = 0

    def record_emission(self, layer: str, chars: int) -> None:
        self.per_layer_emitted[layer] = self.per_layer_emitted.get(layer, 0) + 1
        self.per_layer_chars[layer] = self.per_layer_chars.get(layer, 0) + chars
        self.total_gt_chars_delivered += chars

    def record_suppression(self, layer: str) -> None:
        self.per_layer_suppressed[layer] = self.per_layer_suppressed.get(layer, 0) + 1

    def record_tool_call(self, tool: str) -> None:
        self.per_tool_calls[tool] = self.per_tool_calls.get(tool, 0) + 1

    def record_reaction(self, followed: bool) -> None:
        if followed:
            self.reactions_followed += 1
        else:
            self.reactions_ignored += 1

    def to_dict(self) -> dict[str, Any]:
        elapsed = int(time.time() * 1000) - self.start_ms
        return {
            "task_id": self.task_id,
            "elapsed_ms": elapsed,
            "per_layer_emitted": dict(self.per_layer_emitted),
            "per_layer_suppressed": dict(self.per_layer_suppressed),
            "per_layer_chars": dict(self.per_layer_chars),
            "per_tool_calls": dict(self.per_tool_calls),
            "total_gt_chars_delivered": self.total_gt_chars_delivered,
            "reactions_followed": self.reactions_followed,
            "reactions_ignored": self.reactions_ignored,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)
