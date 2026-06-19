"""Data models for A/B benchmark runs and MCP proof."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ABCondition(Enum):
    """Benchmark condition: MCP availability."""

    NO_MCP = "no_mcp"
    WITH_GROUNDTRUTH_MCP = "with_groundtruth_mcp"


@dataclass
class MCPProof:
    """Evidence that the with_groundtruth_mcp run actually used the MCP server."""

    mcp_enabled: bool = True
    connection_ok: bool = False
    tools_discovered: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    substantive_tool_count: int = 0  # exclude status-only
    valid: bool = False  # True iff connection_ok and substantive_tool_count >= 1
    run_id: str | None = None  # cross-reference with server-side interventions

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "mcp_enabled": self.mcp_enabled,
            "connection_ok": self.connection_ok,
            "tools_discovered": self.tools_discovered,
            "tool_calls": [
                {"name": t.get("name"), "success": t.get("success")}
                for t in self.tool_calls
            ],
            "substantive_tool_count": self.substantive_tool_count,
            "valid": self.valid,
        }
        if self.run_id is not None:
            d["run_id"] = self.run_id
        return d


@dataclass
class RunMetadata:
    """Per-run metadata for A/B comparison."""

    condition: str
    mcp_proof: MCPProof | None = None
    elapsed_s: float = 0.0
    total_cases: int = 0
    total_file_relevance: int = 0
    run_id: str | None = None  # UUID for cross-referencing client and server
    # Model config (for agent-based runs; optional for transport-only runs)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "condition": self.condition,
            "elapsed_s": round(self.elapsed_s, 3),
            "total_cases": self.total_cases,
            "total_file_relevance": self.total_file_relevance,
        }
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.model is not None:
            d["model"] = self.model
        if self.temperature is not None:
            d["temperature"] = self.temperature
        if self.max_tokens is not None:
            d["max_tokens"] = self.max_tokens
        if self.mcp_proof is not None:
            d["mcp_proof"] = self.mcp_proof.to_dict()
        return d


@dataclass
class ABReport:
    """Unified report for one A/B run (one condition)."""

    metadata: RunMetadata
    # Same shape as BenchmarkReport from runner.py for comparison
    total_cases: int = 0
    detected: int = 0
    fix_correct: int = 0
    ai_needed: int = 0
    briefing_would_inform: int = 0
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_relevance_results: list[dict[str, Any]] = field(default_factory=list)
    case_results: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "total_cases": self.total_cases,
            "detected": self.detected,
            "fix_correct": self.fix_correct,
            "ai_needed": self.ai_needed,
            "briefing_would_inform": self.briefing_would_inform,
            "detection_rate": self.detected / self.total_cases if self.total_cases else 0,
            "fix_rate": self.fix_correct / self.total_cases if self.total_cases else 0,
            "elapsed_s": self.elapsed_s,
            "by_category": self.by_category,
            "file_relevance": {
                "count": len(self.file_relevance_results),
                "results": self.file_relevance_results,
            },
            "cases": self.case_results,
        }
