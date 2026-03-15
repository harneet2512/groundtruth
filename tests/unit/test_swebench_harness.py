"""Smoke tests for SWE-bench harness components."""

import json
import pytest
from pathlib import Path

from benchmarks.swebench.config import SWEBenchConfig, AgentMode
from benchmarks.swebench.cost_tracker import CostTracker, TaskCost
from benchmarks.swebench.tools import BASE_TOOLS, GROUNDTRUTH_TOOLS
from benchmarks.swebench.scaffolds import BASELINE_SYSTEM_PROMPT, WITH_GROUNDTRUTH_SYSTEM_PROMPT
from benchmarks.swebench.analyze import wilson_ci


class TestConfig:
    def test_defaults(self):
        config = SWEBenchConfig()
        assert config.model == "gpt-4o-mini"
        assert config.mode == AgentMode.BASELINE
        assert config.max_turns == 30

    def test_run_id(self):
        config = SWEBenchConfig(mode=AgentMode.GROUNDTRUTH)
        assert "groundtruth" in config.run_id
        assert "gpt4omini" in config.run_id

    def test_predictions_path(self):
        config = SWEBenchConfig(mode=AgentMode.BASELINE)
        assert "baseline" in str(config.predictions_path)


class TestCostTracker:
    def test_record_and_total(self):
        ct = CostTracker()
        ct.record("task-1", input_tokens=1000, output_tokens=500)
        ct.record("task-1", input_tokens=2000, output_tokens=1000)
        assert ct.total_input_tokens == 3000
        assert ct.total_output_tokens == 1500
        assert ct.total_cost > 0

    def test_per_task_cost(self):
        ct = CostTracker()
        ct.record("task-1", input_tokens=1_000_000, output_tokens=0)
        # GPT-4o-mini: $0.15 per 1M input tokens
        assert abs(ct.get_task_cost("task-1") - 0.15) < 0.01

    def test_summary(self):
        ct = CostTracker()
        ct.record("task-1", input_tokens=1000, output_tokens=500)
        summary = ct.summary()
        assert summary["total_tasks"] == 1
        assert "task-1" in summary["tasks"]


class TestTools:
    def test_base_tools_count(self):
        assert len(BASE_TOOLS) == 5  # bash, view_file, edit_file, search, submit_patch

    def test_gt_tools_count(self):
        assert len(GROUNDTRUTH_TOOLS) == 15

    def test_tool_format(self):
        for tool in BASE_TOOLS + GROUNDTRUTH_TOOLS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_gt_tool_names(self):
        names = {t["function"]["name"] for t in GROUNDTRUTH_TOOLS}
        assert "groundtruth_orient" in names
        assert "groundtruth_validate" in names
        assert "groundtruth_explain" in names


class TestScaffolds:
    def test_baseline_prompt_exists(self):
        assert len(BASELINE_SYSTEM_PROMPT) > 100

    def test_gt_prompt_exists(self):
        assert len(WITH_GROUNDTRUTH_SYSTEM_PROMPT) > 100

    def test_gt_prompt_mentions_tools(self):
        assert "groundtruth_orient" in WITH_GROUNDTRUTH_SYSTEM_PROMPT
        assert "groundtruth_validate" in WITH_GROUNDTRUTH_SYSTEM_PROMPT

    def test_baseline_no_gt_tools(self):
        assert "groundtruth_" not in BASELINE_SYSTEM_PROMPT


class TestAnalysis:
    def test_wilson_ci_basic(self):
        low, high = wilson_ci(50, 100)
        assert 0.35 < low < 0.45
        assert 0.55 < high < 0.65

    def test_wilson_ci_zero(self):
        low, high = wilson_ci(0, 0)
        assert low == 0.0
        assert high == 0.0

    def test_wilson_ci_perfect(self):
        low, high = wilson_ci(100, 100)
        assert low > 0.95
        assert high > 0.99
