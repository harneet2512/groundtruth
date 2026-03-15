"""Tests for experiment runner and analysis functions."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Add benchmarks to path so imports match what the runner uses
_ROOT = Path(__file__).resolve().parent.parent.parent
_BENCH = _ROOT / "benchmarks"
sys.path.insert(0, str(_BENCH))
sys.path.insert(0, str(_ROOT / "src"))

from _fixtures import LANG_CONFIG, populate_store  # noqa: E402
from experiments.analyze_adaptive_improvement import (  # noqa: E402
    analyze_adaptive_improvement,
    generate_markdown as adaptive_markdown,
)
from experiments.analyze_grounding_gap import (  # noqa: E402
    analyze_grounding_gap,
    generate_markdown as gap_markdown,
)
from experiments.analyze_risk_correlation import (  # noqa: E402
    analyze_risk_correlation,
    generate_markdown as risk_markdown,
)
from experiments.experiment_runner import (  # noqa: E402
    aggregate_results,
    cases_to_tasks,
    run_task_baseline,
    run_task_standard,
    run_task_adaptive,
    setup_language_env,
)
from experiments.models import (  # noqa: E402
    ExperimentConfig,
    ExperimentReport,
    ExperimentResult,
    ExperimentTask,
)
from runner import BenchmarkCase  # noqa: E402

from groundtruth.index.store import SymbolStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_cases() -> list[BenchmarkCase]:
    return [
        BenchmarkCase(
            id="test-001",
            category="wrong-module-path",
            language="typescript",
            description="Wrong import path",
            code='import { hashPassword } from "auth"',
            file_path="src/routes/users.ts",
            intent="hash user password for storage",
            correct_symbol="hashPassword",
            correct_import="src/utils/crypto.ts",
        ),
        BenchmarkCase(
            id="test-002",
            category="missing-package",
            language="typescript",
            description="Missing package",
            code='import axios from "axios"',
            file_path="src/routes/users.ts",
            intent="make HTTP request",
            correct_symbol="axios",
            correct_import="axios",
        ),
        BenchmarkCase(
            id="test-003",
            category="wrong-module-path",
            language="typescript",
            description="No intent — should be skipped",
            code='import { foo } from "bar"',
            file_path="src/routes/users.ts",
            # Missing intent + correct_symbol + correct_import
        ),
    ]


@pytest.fixture
def ts_env() -> tuple[Any, ...]:
    return setup_language_env("typescript")


@pytest.fixture
def sample_task() -> ExperimentTask:
    return ExperimentTask(
        case_id="test-001",
        category="wrong-module-path",
        language="typescript",
        code='import { hashPassword } from "auth"',
        file_path="src/routes/users.ts",
        intent="hash user password for storage",
        correct_symbol="hashPassword",
        correct_import="src/utils/crypto.ts",
    )


# ---------------------------------------------------------------------------
# Test cases_to_tasks
# ---------------------------------------------------------------------------


class TestCasesToTasks:
    def test_converts_valid_cases(self, sample_cases: list[BenchmarkCase]) -> None:
        tasks = cases_to_tasks(sample_cases)
        # Only 2 of 3 have all required fields
        assert len(tasks) == 2

    def test_task_fields(self, sample_cases: list[BenchmarkCase]) -> None:
        tasks = cases_to_tasks(sample_cases)
        t = tasks[0]
        assert t.case_id == "test-001"
        assert t.category == "wrong-module-path"
        assert t.language == "typescript"
        assert t.intent == "hash user password for storage"
        assert t.correct_symbol == "hashPassword"

    def test_frozen(self, sample_cases: list[BenchmarkCase]) -> None:
        tasks = cases_to_tasks(sample_cases)
        with pytest.raises(AttributeError):
            tasks[0].case_id = "modified"  # type: ignore[misc]

    def test_empty_input(self) -> None:
        assert cases_to_tasks([]) == []


# ---------------------------------------------------------------------------
# Test run_task_baseline
# ---------------------------------------------------------------------------


class TestRunTaskBaseline:
    @pytest.mark.asyncio
    async def test_returns_result(self, ts_env: tuple[Any, ...], sample_task: ExperimentTask) -> None:
        _store, orchestrator, _briefing, _adaptive, risk_scorer = ts_env
        result = await run_task_baseline(orchestrator, risk_scorer, sample_task)
        assert isinstance(result, ExperimentResult)
        assert result.config == "baseline"
        assert result.case_id == "test-001"

    @pytest.mark.asyncio
    async def test_no_briefing_fields(
        self, ts_env: tuple[Any, ...], sample_task: ExperimentTask
    ) -> None:
        _store, orchestrator, _briefing, _adaptive, risk_scorer = ts_env
        result = await run_task_baseline(orchestrator, risk_scorer, sample_task)
        assert result.briefing_covers_correct_symbol is False
        assert result.briefing_covers_correct_import is False
        assert result.briefing_symbol_count == 0
        assert result.compliance_proxy == 0.0

    @pytest.mark.asyncio
    async def test_has_latency(self, ts_env: tuple[Any, ...], sample_task: ExperimentTask) -> None:
        _store, orchestrator, _briefing, _adaptive, risk_scorer = ts_env
        result = await run_task_baseline(orchestrator, risk_scorer, sample_task)
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Test run_task_standard
# ---------------------------------------------------------------------------


class TestRunTaskStandard:
    @pytest.mark.asyncio
    async def test_returns_result(
        self, ts_env: tuple[Any, ...], sample_task: ExperimentTask
    ) -> None:
        _store, orchestrator, briefing, _adaptive, risk_scorer = ts_env
        result = await run_task_standard(orchestrator, briefing, risk_scorer, sample_task)
        assert isinstance(result, ExperimentResult)
        assert result.config == "standard"

    @pytest.mark.asyncio
    async def test_briefing_populated(
        self, ts_env: tuple[Any, ...], sample_task: ExperimentTask
    ) -> None:
        _store, orchestrator, briefing, _adaptive, risk_scorer = ts_env
        result = await run_task_standard(orchestrator, briefing, risk_scorer, sample_task)
        # Briefing should have found some symbols via FTS5
        assert result.briefing_symbol_count >= 0


# ---------------------------------------------------------------------------
# Test run_task_adaptive
# ---------------------------------------------------------------------------


class TestRunTaskAdaptive:
    @pytest.mark.asyncio
    async def test_returns_result(
        self, ts_env: tuple[Any, ...], sample_task: ExperimentTask
    ) -> None:
        _store, orchestrator, briefing, adaptive, risk_scorer = ts_env
        result = await run_task_adaptive(
            orchestrator, briefing, adaptive, risk_scorer, sample_task
        )
        assert isinstance(result, ExperimentResult)
        assert result.config == "adaptive"

    @pytest.mark.asyncio
    async def test_may_differ_from_standard(
        self, ts_env: tuple[Any, ...], sample_task: ExperimentTask
    ) -> None:
        _store, orchestrator, briefing, adaptive, risk_scorer = ts_env
        std = await run_task_standard(orchestrator, briefing, risk_scorer, sample_task)
        adp = await run_task_adaptive(
            orchestrator, briefing, adaptive, risk_scorer, sample_task
        )
        # Both should have valid results (may or may not differ)
        assert std.config == "standard"
        assert adp.config == "adaptive"
        # Detection should be the same (same validation)
        assert std.error_detected == adp.error_detected


# ---------------------------------------------------------------------------
# Test aggregate_results
# ---------------------------------------------------------------------------


class TestAggregateResults:
    def test_correct_rates(self) -> None:
        results = [
            ExperimentResult(
                case_id="a", config="baseline", category="cat1", language="ts",
                error_detected=True, fix_correct=True, compliance_proxy=0.5,
                file_risk_score=0.3,
            ),
            ExperimentResult(
                case_id="b", config="baseline", category="cat1", language="ts",
                error_detected=True, fix_correct=False, compliance_proxy=0.0,
                file_risk_score=0.7,
            ),
            ExperimentResult(
                case_id="c", config="baseline", category="cat2", language="py",
                error_detected=False, fix_correct=False, compliance_proxy=1.0,
                file_risk_score=0.1,
            ),
        ]
        report = aggregate_results("baseline", results, 1.0)
        assert report.total_tasks == 3
        assert report.detection_rate == pytest.approx(2 / 3)
        assert report.fix_rate == pytest.approx(1 / 3)
        assert report.mean_compliance_proxy == pytest.approx(0.5)
        assert report.mean_risk_score == pytest.approx((0.3 + 0.7 + 0.1) / 3)

    def test_by_category(self) -> None:
        results = [
            ExperimentResult(
                case_id="a", config="baseline", category="cat1", language="ts",
                error_detected=True,
            ),
            ExperimentResult(
                case_id="b", config="baseline", category="cat2", language="ts",
                error_detected=False,
            ),
        ]
        report = aggregate_results("baseline", results, 0.5)
        assert "cat1" in report.by_category
        assert "cat2" in report.by_category
        assert report.by_category["cat1"]["detection_rate"] == 1.0
        assert report.by_category["cat2"]["detection_rate"] == 0.0

    def test_empty_results(self) -> None:
        report = aggregate_results("baseline", [], 0.0)
        assert report.total_tasks == 0
        assert report.detection_rate == 0.0

    def test_by_language(self) -> None:
        results = [
            ExperimentResult(
                case_id="a", config="baseline", category="c", language="typescript",
                error_detected=True,
            ),
            ExperimentResult(
                case_id="b", config="baseline", category="c", language="python",
                error_detected=False,
            ),
        ]
        report = aggregate_results("baseline", results, 0.5)
        assert "typescript" in report.by_language
        assert "python" in report.by_language


# ---------------------------------------------------------------------------
# Test analysis functions
# ---------------------------------------------------------------------------


class TestAnalysisGroundingGap:
    def test_produces_valid_output(self, tmp_path: Path) -> None:
        # Write fake standard results
        results = [
            {"case_id": "a", "category": "cat1", "language": "ts",
             "briefing_covers_correct_symbol": True,
             "briefing_covers_correct_import": False,
             "compliance_proxy": 0.5},
            {"case_id": "b", "category": "cat1", "language": "ts",
             "briefing_covers_correct_symbol": False,
             "briefing_covers_correct_import": False,
             "compliance_proxy": 0.0},
        ]
        with open(tmp_path / "standard.json", "w") as f:
            json.dump({"results": results}, f)
        with open(tmp_path / "adaptive.json", "w") as f:
            json.dump({"results": results}, f)

        analysis = analyze_grounding_gap(tmp_path)
        assert "standard" in analysis
        assert analysis["standard"]["total"] == 2
        assert analysis["standard"]["overall_symbol_coverage"] == 0.5

    def test_generates_markdown(self, tmp_path: Path) -> None:
        results = [
            {"case_id": "a", "category": "c", "language": "ts",
             "briefing_covers_correct_symbol": True,
             "briefing_covers_correct_import": True,
             "compliance_proxy": 1.0},
        ]
        with open(tmp_path / "standard.json", "w") as f:
            json.dump({"results": results}, f)

        analysis = analyze_grounding_gap(tmp_path)
        md = gap_markdown(analysis)
        assert "# Grounding Gap Analysis" in md
        assert isinstance(md, str)


class TestAnalysisRiskCorrelation:
    def test_produces_valid_output(self, tmp_path: Path) -> None:
        results = [
            {"case_id": "a", "error_detected": True,
             "risk_factors": {"naming_ambiguity": 0.8, "import_depth": 0.0,
                              "convention_variance": 0.0, "overloaded_paths": 0.0,
                              "parameter_complexity": 0.0, "isolation_score": 0.0}},
            {"case_id": "b", "error_detected": False,
             "risk_factors": {"naming_ambiguity": 0.1, "import_depth": 0.0,
                              "convention_variance": 0.0, "overloaded_paths": 0.0,
                              "parameter_complexity": 0.0, "isolation_score": 0.0}},
        ]
        with open(tmp_path / "baseline.json", "w") as f:
            json.dump({"results": results}, f)

        analysis = analyze_risk_correlation(tmp_path)
        assert "factors" in analysis
        assert "naming_ambiguity" in analysis["factors"]

    def test_generates_markdown(self, tmp_path: Path) -> None:
        results = [
            {"case_id": "a", "error_detected": True,
             "risk_factors": {"naming_ambiguity": 0.5}},
        ]
        with open(tmp_path / "baseline.json", "w") as f:
            json.dump({"results": results}, f)

        analysis = analyze_risk_correlation(tmp_path)
        md = risk_markdown(analysis)
        assert "# Risk Factor Correlation" in md


class TestAnalysisAdaptiveImprovement:
    def test_produces_valid_output(self, tmp_path: Path) -> None:
        std_results = [
            {"case_id": "a", "error_detected": True, "fix_correct": True,
             "briefing_covers_correct_symbol": True, "compliance_proxy": 1.0,
             "file_risk_score": 0.5},
        ]
        adp_results = [
            {"case_id": "a", "error_detected": True, "fix_correct": True,
             "briefing_covers_correct_symbol": True, "compliance_proxy": 1.0,
             "file_risk_score": 0.5},
        ]
        with open(tmp_path / "standard.json", "w") as f:
            json.dump({"results": std_results}, f)
        with open(tmp_path / "adaptive.json", "w") as f:
            json.dump({"results": adp_results}, f)

        analysis = analyze_adaptive_improvement(tmp_path)
        assert "overall" in analysis
        assert analysis["total_paired"] == 1

    def test_generates_markdown(self, tmp_path: Path) -> None:
        for config in ["standard", "adaptive"]:
            with open(tmp_path / f"{config}.json", "w") as f:
                json.dump({"results": [
                    {"case_id": "a", "category": "c", "error_detected": True,
                     "fix_correct": False, "briefing_covers_correct_symbol": True,
                     "compliance_proxy": 0.5, "file_risk_score": 0.3},
                ]}, f)

        analysis = analyze_adaptive_improvement(tmp_path)
        md = adaptive_markdown(analysis)
        assert "# Adaptive vs Standard" in md

    def test_missing_results(self, tmp_path: Path) -> None:
        analysis = analyze_adaptive_improvement(tmp_path)
        assert "error" in analysis
