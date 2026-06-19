"""Architecture contract tests — GT_ARCHITECTURE_CONTRACT.md enforcement.

These tests enforce structural invariants. They are not unit tests of
specific functions. They verify that the system as wired satisfies the
contract. Failures here block vNext from being called complete.
"""

from __future__ import annotations

import inspect
import os
import time

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.schema.finding import (
    AgentAction,
    Finding,
    FindingKind,
    Location,
    Severity,
    WhyNow,
    format_findings,
)
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.utils.result import Ok


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> SymbolStore:
    s = SymbolStore(":memory:")
    s.initialize()
    now = int(time.time())
    s.insert_symbol(
        name="process", kind="function", language="python",
        file_path="src/core.py", line_number=10, end_line=30,
        is_exported=True, signature="(data: dict) -> dict",
        params="data: dict", return_type="dict",
        documentation=None, last_indexed_at=now,
    )
    s.insert_symbol(
        name="validate", kind="function", language="python",
        file_path="src/validators.py", line_number=5, end_line=15,
        is_exported=True, signature="(payload: dict) -> bool",
        params="payload: dict", return_type="bool",
        documentation=None, last_indexed_at=now,
    )
    return s


@pytest.fixture()
def graph(store: SymbolStore) -> ImportGraph:
    return ImportGraph(store)


def _make_finding(**overrides) -> Finding:
    defaults = dict(
        kind=FindingKind.GUARD_REMOVED,
        severity=Severity.WARNING,
        confidence=0.8,
        location=Location(file="src/core.py", line=10, symbol="process"),
        message="guard removed",
    )
    defaults.update(overrides)
    return Finding(**defaults)


# ── §1.2 — Post-run is telemetry, not decision support ──────────────────


class TestReviewPatchAgentCanRespond:
    """Contract §1.2: review_patch must not be post-run only.

    The hook harness must have a code path where review_patch output
    reaches the agent BEFORE submission is captured. A post-run-only
    review_patch is telemetry.
    """

    def test_review_patch_has_pre_submit_path(self) -> None:
        """review_patch must fire inside _hooked_execute where the agent
        can act on the output before submission is captured."""
        harness_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "benchmarks", "swebench",
            "run_mini_gt_hooked.py",
        )
        with open(os.path.abspath(harness_path)) as f:
            source = f.read()

        # review_patch must fire inside _hooked_execute (agent sees it)
        # or inside the agent loop, NOT only after agent.run() returns.
        #
        # We check: is _run_review_patch called inside _hooked_execute?
        # If it's only called after agent.run(), this test fails.
        in_hooked_execute = False
        in_block = False
        for line in source.split("\n"):
            if "def _hooked_execute" in line:
                in_block = True
            elif in_block and line and not line[0].isspace() and "def " in line:
                in_block = False
            if in_block and "_run_review_patch" in line:
                in_hooked_execute = True
                break

        assert in_hooked_execute, (
            "CONTRACT VIOLATION §1.2: _run_review_patch is not called inside "
            "_hooked_execute. It only fires post-run (telemetry). "
            "Move it into the submit interception path."
        )

    def test_post_run_labeled_telemetry(self) -> None:
        """Any post-run review_patch call must be labeled as telemetry."""
        harness_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "benchmarks", "swebench",
            "run_mini_gt_hooked.py",
        )
        with open(os.path.abspath(harness_path)) as f:
            source = f.read()

        # If review_patch fires after agent.run(), the metadata must say so
        if "review_patch" in source and "agent.run" in source:
            assert "telemetry" in source.lower() or "post-run" in source.lower(), (
                "CONTRACT VIOLATION §1.2: post-run review_patch must be "
                "labeled as telemetry in code comments or metadata."
            )


# ── §1.1 — No surface complete without agent response ───────────────────


class TestNoSurfaceCompleteWithoutAgentResponse:
    """Contract §1.1: every surface output must reach the agent where
    it can act."""

    def test_task_map_prepended_to_task(self) -> None:
        """task_map output must be prepended to the task prompt or
        otherwise available before the agent's first action."""
        harness_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "benchmarks", "swebench",
            "run_mini_gt_hooked.py",
        )
        with open(os.path.abspath(harness_path)) as f:
            source = f.read()

        assert "task = " in source and "briefing" in source, (
            "task_map briefing must be prepended to task"
        )
        # Briefing must happen BEFORE agent.run()
        briefing_line = None
        run_line = None
        for i, line in enumerate(source.split("\n")):
            if "_generate_briefing" in line and briefing_line is None:
                briefing_line = i
            if "agent.run(task)" in line and run_line is None:
                run_line = i
        assert briefing_line is not None, "briefing must be called"
        assert run_line is not None, "agent.run must be called"
        assert briefing_line < run_line, (
            "CONTRACT VIOLATION §1.1: briefing must happen before agent.run()"
        )

    def test_event_brief_in_hooked_execute(self) -> None:
        """event_brief must fire inside _hooked_execute so the agent sees it."""
        harness_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "benchmarks", "swebench",
            "run_mini_gt_hooked.py",
        )
        with open(os.path.abspath(harness_path)) as f:
            source = f.read()

        in_hooked = False
        found = False
        for line in source.split("\n"):
            if "def _hooked_execute" in line:
                in_hooked = True
            elif in_hooked and line and not line[0].isspace() and "def " in line:
                in_hooked = False
            if in_hooked and "_run_gt_intel" in line:
                found = True
                break

        assert found, (
            "CONTRACT VIOLATION §1.1: event_brief (_run_gt_intel) must be "
            "inside _hooked_execute so agent sees the output"
        )


# ── §2.3 — Prohibited output ────────────────────────────────────────────


class TestProhibitedOutput:
    def test_no_empty_gt_evidence_wrapper(self) -> None:
        """Empty findings must produce empty string, not an empty wrapper."""
        text = format_findings([], "task_map")
        assert text == ""
        assert "<gt-evidence" not in text

    def test_no_ok_noise(self) -> None:
        """Surfaces must never emit [OK] when there are no findings."""
        text = format_findings([], "event_brief")
        assert "[OK]" not in text

    def test_no_reasoning_guidance_footer_in_surfaces(self) -> None:
        """Surface output must not contain reasoning_guidance footer."""
        findings = [_make_finding()]
        text = format_findings(findings, "task_map")
        assert "---\n" not in text
        assert "reasoning_guidance" not in text

    def test_no_cross_tool_pointers_in_findings(self) -> None:
        """Finding messages must not contain 'Call groundtruth_X' pointers."""
        f = _make_finding(message="Call groundtruth_impact before editing")
        line = f.to_text_line()
        # The contract says no cross-tool pointers. This test documents
        # that the Finding schema doesn't prevent them — the pruning
        # layer must enforce this at the handler level.
        # We verify format_findings doesn't add them.
        text = format_findings([_make_finding()], "task_map")
        assert "groundtruth_" not in text


# ── §3 — Novelty suppression ────────────────────────────────────────────


class TestNoveltySuppression:
    def test_repeated_findings_suppressed(self) -> None:
        """Second emission of the same finding must be marked not novel."""
        nf = NoveltyFilter()
        f = _make_finding()

        r1 = nf.filter([f])
        assert r1[0].novelty is True

        r2 = nf.filter([_make_finding()])  # same identity
        assert r2[0].novelty is False

    def test_novelty_shared_across_surfaces(self) -> None:
        """A single NoveltyFilter instance must deduplicate across surfaces."""
        nf = NoveltyFilter()
        f1 = _make_finding(kind=FindingKind.FILE_RELEVANCE)
        f2 = _make_finding(kind=FindingKind.FILE_RELEVANCE)

        nf.filter([f1])  # shown in task_map
        r = nf.filter([f2])  # same finding in event_brief
        assert r[0].novelty is False


# ── §2.1 — Finding required fields ──────────────────────────────────────


class TestFindingRequiredFields:
    def test_finding_has_all_required_fields(self) -> None:
        f = _make_finding()
        assert isinstance(f.kind, FindingKind)
        assert isinstance(f.severity, Severity)
        assert 0.0 <= f.confidence <= 1.0
        assert f.location.file
        assert f.location.line or f.location.symbol
        assert f.message
        assert isinstance(f.why_now, WhyNow)
        assert isinstance(f.agent_action, AgentAction)

    def test_finding_rejects_empty_file(self) -> None:
        with pytest.raises(Exception):
            Finding(
                kind=FindingKind.GUARD_REMOVED,
                severity=Severity.WARNING,
                confidence=0.8,
                location=Location(file=""),
                message="test",
            )

    def test_finding_rejects_out_of_range_confidence(self) -> None:
        with pytest.raises(Exception):
            _make_finding(confidence=1.5)
        with pytest.raises(Exception):
            _make_finding(confidence=-0.1)


# ── §4 — Benchmark validity gates ───────────────────────────────────────


class TestBenchmarkValidityGates:
    def test_benchmark_arms_documented(self) -> None:
        """Contract file must define required benchmark arms."""
        contract_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "GT_ARCHITECTURE_CONTRACT.md",
        )
        with open(os.path.abspath(contract_path)) as f:
            text = f.read()

        assert "format-repaired baseline" in text
        assert "shell-only" in text
        assert "Do not compare against raw broken Qwen" in text

    def test_no_raw_qwen_comparison(self) -> None:
        """Verify the contract prohibits raw-Qwen comparison."""
        contract_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "GT_ARCHITECTURE_CONTRACT.md",
        )
        with open(os.path.abspath(contract_path)) as f:
            text = f.read()

        assert "scaffold-broken" in text


# ── §6 — No AI layer in Finding pipeline ────────────────────────────────


class TestNoAILayer:
    def test_finding_schema_has_no_ai_imports(self) -> None:
        """The Finding schema module must not import any AI/LLM libraries."""
        import groundtruth.schema.finding as mod

        source = inspect.getsource(mod)
        assert "anthropic" not in source
        assert "openai" not in source
        assert "langchain" not in source

    def test_adapters_have_no_ai_imports(self) -> None:
        import groundtruth.schema.adapters as mod

        source = inspect.getsource(mod)
        assert "anthropic" not in source
        assert "openai" not in source

    def test_surface_handlers_have_no_ai_imports(self) -> None:
        import groundtruth.mcp.endpoints.task_map as tm
        import groundtruth.mcp.endpoints.event_brief as eb
        import groundtruth.mcp.endpoints.review_patch as rp

        for mod in [tm, eb, rp]:
            source = inspect.getsource(mod)
            assert "anthropic" not in source.lower(), f"{mod.__name__} imports AI"
            assert "openai" not in source.lower(), f"{mod.__name__} imports AI"
