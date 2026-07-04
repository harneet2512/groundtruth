"""Stress tests for ``kernel.brief`` Boundary 1 projection.

Layers per locked decision 6: happy / boundary / adversarial / mutation.
The Boundary 1 contract is the leakage gate: anything from V1RBriefResult
that is NOT in BriefResult must be dropped, no exceptions.

Mocks the underlying ``pretask.v1r_brief.generate_v1r_brief`` because we are
testing the projection layer, not the brief pipeline (the brief layer has
its own 70+ tests in ``tests/pretask/``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from groundtruth.control import kernel
from groundtruth.control.types import BriefResult, TaskInput
from groundtruth.pretask.v1r_brief import FileEntry, V1RBriefResult


_DEFAULT = object()


def _make_v1r_result(
    *,
    brief_text: str = "edit src/foo.py",
    files: object = _DEFAULT,
) -> V1RBriefResult:
    if files is _DEFAULT:
        files = [
            FileEntry(path="src/foo.py", score=0.9, functions=["foo"], contract="must return User"),
            FileEntry(path="src/bar.py", score=0.4, functions=["bar"]),
        ]
    return V1RBriefResult(
        files=files,  # type: ignore[arg-type]
        brief_text=brief_text,
        token_estimate=10,
        confidence_tier="high",
        graph_edge_count=1,
        semantic_signal_count=1,
        structural_signal_count=1,
        fts5_signal_count=1,
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
    with patch("groundtruth.pretask.v1r_brief.generate_v1r_brief", return_value=_make_v1r_result()):
        result = kernel.brief(_task())
    assert isinstance(result, BriefResult)
    assert result.brief_text == "edit src/foo.py"
    assert result.confidence == 0.9
    assert len(result.candidates) == 2
    assert result.candidates[0].path == Path("src/foo.py")
    assert result.candidates[0].score == 0.9


# boundary
def test_boundary_focus_files_capped_at_3() -> None:
    files = [FileEntry(path=f"{name}.py", score=0.5) for name in ("a", "b", "c", "d", "e")]
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(files=files),
    ):
        result = kernel.brief(_task())
    assert len(result.focus_files) == 3
    assert result.focus_files == [Path("a.py"), Path("b.py"), Path("c.py")]


def test_boundary_path_normalization_strips_workspace_prefix() -> None:
    files = [FileEntry(path="testbed/src/foo.py", score=0.7)]
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(files=files),
    ):
        result = kernel.brief(_task())
    assert result.focus_files == [Path("src/foo.py")]
    assert result.candidates[0].path == Path("src/foo.py")


def test_boundary_empty_lists_default_to_empty() -> None:
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(files=[]),
    ):
        result = kernel.brief(_task())
    assert result.focus_files == []
    assert result.cluster_files == []
    assert result.candidates == []


# adversarial -- the leakage cases
def test_adversarial_plan_path_dropped() -> None:
    """plan_path is host-only -- must not cross Boundary 1."""
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(),
    ):
        result = kernel.brief(_task())
    assert result.plan_path is None


def test_adversarial_telemetry_not_in_result() -> None:
    """V1R internals carry signal provenance -- must not leak as raw telemetry."""
    with patch("groundtruth.pretask.v1r_brief.generate_v1r_brief", return_value=_make_v1r_result()):
        result = kernel.brief(_task())
    # BriefResult has no `telemetry` field; pydantic extra='forbid' would
    # raise if the wrap tried to set it. Asserts the field is genuinely absent.
    assert "telemetry" not in result.model_dump()


def test_adversarial_candidate_tags_dropped() -> None:
    """pretask.render.Candidate carries ``tags`` and ``is_test``; must not leak."""
    files = [
        FileEntry(
            path="src/foo.py",
            score=0.9,
            witness="foo calls bar",
            witness_verified=True,
            localizer_confidence=0.8,
        )
    ]
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(files=files),
    ):
        result = kernel.brief(_task())
    fields = set(result.candidates[0].model_dump().keys())
    assert fields == {"path", "score"}


def test_adversarial_string_brief_raises() -> None:
    """If generate_v1r_brief returns ``str`` the
    contract is broken; kernel must surface it loudly, not silently fall
    through to a partial BriefResult.
    """
    with patch("groundtruth.pretask.v1r_brief.generate_v1r_brief", return_value="raw text"):
        with pytest.raises(RuntimeError, match="contract drift"):
            kernel.brief(_task())


# mutation pin -- if focus_files cap is removed (e.g. drops [:3]) this fails
def test_mutation_pin_focus_files_max_length() -> None:
    files = [FileEntry(path=f"{name}.py", score=0.5) for name in ("a", "b", "c", "d")]
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(files=files),
    ):
        result = kernel.brief(_task())
    assert len(result.focus_files) <= 3


# mutation pin -- confidence must come from delivered v1r entries, not hidden
# telemetry or a stale v7 plan.
def test_mutation_pin_confidence_from_plan() -> None:
    files = [FileEntry(path="src/foo.py", score=0.42)]
    with patch(
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        return_value=_make_v1r_result(files=files),
    ):
        result = kernel.brief(_task())
    assert result.confidence == 0.42
