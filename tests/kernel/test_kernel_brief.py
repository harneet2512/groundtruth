"""Stress tests for ``kernel.brief`` Boundary 1 projection.

Layers per locked decision 6: happy / boundary / adversarial / mutation.
The Boundary 1 contract is the leakage gate: anything from V7BriefResult
that is NOT in BriefResult must be dropped, no exceptions.

Mocks the underlying ``pretask.v7_brief.generate_brief`` because we are
testing the projection layer, not the brief pipeline (the brief layer has
its own 70+ tests in ``tests/pretask/``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.control import kernel
from groundtruth.control.types import BriefResult, TaskInput
from groundtruth.pretask.render import Candidate as PretaskCandidate
from groundtruth.pretask.v7_brief import V7BriefResult


_DEFAULT = object()


def _make_v7_result(
    *,
    brief_text: str = "edit src/foo.py",
    plan: object = _DEFAULT,
    candidates: object = _DEFAULT,
    cluster_files: object = _DEFAULT,
    plan_path: str | None = "/host/path/plan.json",
) -> V7BriefResult:
    if plan is _DEFAULT:
        plan = {
            "agent_focus_files": ["src/foo.py"],
            "contract_lines": ["must return User"],
            "constraints": ["edit cluster first"],
            "confidence": 0.71,
        }
    if candidates is _DEFAULT:
        candidates = [PretaskCandidate(file="src/foo.py", score=0.9)]
    if cluster_files is _DEFAULT:
        cluster_files = ["src/foo.py", "src/bar.py"]
    return V7BriefResult(
        brief=brief_text,
        telemetry=MagicMock(name="V7BriefResult.telemetry"),
        telemetry_path="/host/path/telemetry.jsonl",
        candidates=candidates,  # type: ignore[arg-type]
        cluster_files=cluster_files,  # type: ignore[arg-type]
        plan=plan,  # type: ignore[arg-type]
        plan_path=plan_path,
    )


def _task() -> TaskInput:
    return TaskInput(
        task_id="t1",
        repo_root=Path("/tmp/repo"),
        issue_text="fix it",
        base_commit="HEAD",
    )


# happy
def test_happy_basic_projection() -> None:
    with patch("groundtruth.pretask.v7_brief.generate_brief", return_value=_make_v7_result()):
        result = kernel.brief(_task())
    assert isinstance(result, BriefResult)
    assert result.brief_text == "edit src/foo.py"
    assert result.confidence == 0.71
    assert len(result.candidates) == 1
    assert result.candidates[0].path == Path("src/foo.py")
    assert result.candidates[0].score == 0.9


# boundary
def test_boundary_focus_files_capped_at_3() -> None:
    plan = {
        "agent_focus_files": ["a.py", "b.py", "c.py", "d.py", "e.py"],
        "contract_lines": [],
        "constraints": [],
        "confidence": 0.5,
    }
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(plan=plan),
    ):
        result = kernel.brief(_task())
    assert len(result.focus_files) == 3
    assert result.focus_files == [Path("a.py"), Path("b.py"), Path("c.py")]


def test_boundary_path_normalization_strips_workspace_prefix() -> None:
    plan = {
        "agent_focus_files": ["workspace/src/foo.py"],
        "contract_lines": [],
        "constraints": [],
        "confidence": 0.5,
    }
    cands = [PretaskCandidate(file="testbed/src/foo.py", score=0.7)]
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(plan=plan, candidates=cands),
    ):
        result = kernel.brief(_task())
    assert result.focus_files == [Path("src/foo.py")]
    assert result.candidates[0].path == Path("src/foo.py")


def test_boundary_empty_lists_default_to_empty() -> None:
    plan = {
        "agent_focus_files": [],
        "contract_lines": [],
        "constraints": [],
        "confidence": 0.0,
    }
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(plan=plan, candidates=[], cluster_files=[]),
    ):
        result = kernel.brief(_task())
    assert result.focus_files == []
    assert result.cluster_files == []
    assert result.candidates == []


# adversarial -- the leakage cases
def test_adversarial_plan_path_dropped() -> None:
    """plan_path is host-only -- must not cross Boundary 1."""
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(plan_path="/host/secret/path/plan.json"),
    ):
        result = kernel.brief(_task())
    assert result.plan_path is None


def test_adversarial_telemetry_not_in_result() -> None:
    """V7BriefResult.telemetry carries internal module scores -- must not leak."""
    with patch("groundtruth.pretask.v7_brief.generate_brief", return_value=_make_v7_result()):
        result = kernel.brief(_task())
    # BriefResult has no `telemetry` field; pydantic extra='forbid' would
    # raise if the wrap tried to set it. Asserts the field is genuinely absent.
    assert "telemetry" not in result.model_dump()


def test_adversarial_candidate_tags_dropped() -> None:
    """pretask.render.Candidate carries ``tags`` and ``is_test``; must not leak."""
    cands = [
        PretaskCandidate(
            file="src/foo.py",
            score=0.9,
            tags=[("anchor", "issue_text")],
            is_test=False,
        )
    ]
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(candidates=cands),
    ):
        result = kernel.brief(_task())
    fields = set(result.candidates[0].model_dump().keys())
    assert fields == {"path", "score"}


def test_adversarial_string_brief_raises() -> None:
    """If generate_brief returns ``str`` despite return_telemetry=True the
    contract is broken; kernel must surface it loudly, not silently fall
    through to a partial BriefResult.
    """
    with patch("groundtruth.pretask.v7_brief.generate_brief", return_value="raw text"):
        with pytest.raises(RuntimeError, match="contract drift"):
            kernel.brief(_task())


# mutation pin -- if focus_files cap is removed (e.g. drops [:3]) this fails
def test_mutation_pin_focus_files_max_length() -> None:
    plan = {
        "agent_focus_files": ["a.py", "b.py", "c.py", "d.py"],
        "contract_lines": [],
        "constraints": [],
        "confidence": 0.5,
    }
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(plan=plan),
    ):
        result = kernel.brief(_task())
    assert len(result.focus_files) <= 3


# mutation pin -- if confidence comes from V7BriefResult.telemetry instead of
# plan["confidence"] this would break (telemetry has no confidence field)
def test_mutation_pin_confidence_from_plan() -> None:
    plan = {
        "agent_focus_files": [],
        "contract_lines": [],
        "constraints": [],
        "confidence": 0.42,
    }
    with patch(
        "groundtruth.pretask.v7_brief.generate_brief",
        return_value=_make_v7_result(plan=plan),
    ):
        result = kernel.brief(_task())
    assert result.confidence == 0.42
