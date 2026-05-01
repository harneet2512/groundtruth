"""Stress tests for ``kernel.observe_edit`` Boundary 1 projection.

Mocks ``runtime.patch_auditor.audit_patch`` because patch_auditor has its
own 30+ tests; we test the projection layer here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from groundtruth.control import kernel
from groundtruth.control.types import (
    BriefResult,
    Capabilities,
    EditEvent,
    EditObservation,
    RunState,
)


def _run_state() -> RunState:
    return RunState(
        task_id="t1",
        plan={
            "agent_focus_files": ["src/a.py"],
            "cluster_files": ["src/a.py", "src/b.py"],
        },
        brief_result=BriefResult(
            brief_text="x",
            candidates=[],
            focus_files=[Path("src/a.py")],
            cluster_files=[Path("src/a.py")],
            confidence=0.7,
            plan={},
        ),
        capabilities=Capabilities(
            block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True
        ),
    )


def _edit(files: list[str]) -> EditEvent:
    return EditEvent(
        task_id="t1",
        files_changed=[Path(f) for f in files],
        diff_text="--- diff text ---",
        ts="2026-05-01T00:00:00Z",
        source_tool="str_replace_editor",
    )


def _audit_return(**overrides) -> dict:
    base = {
        "source_files_touched": ["src/a.py"],
        "test_files_touched": [],
        "root_scaffold_files_added": [],
        "cluster_files_touched": ["src/a.py"],
        "cluster_touch_rate": 1.0,
        "agent_focus_files_touched": ["src/a.py"],
        "focus_hit_at_1": True,
        "focus_hit_at_3": True,
        "outside_cluster_files": [],
        "forbidden_files_touched": [],
        "expected_side_files_missing": [],
        "warnings": [],
        "recommendation": "on_plan",
        "test_execution": {"executed": False, "passed": 0, "failed": 0, "errored": 0, "all_passed": False},
    }
    base.update(overrides)
    return base


# happy
def test_happy_focus_hit() -> None:
    with patch("groundtruth.runtime.patch_auditor.audit_patch", return_value=_audit_return()):
        obs = kernel.observe_edit(_edit(["src/a.py"]), _run_state())
    assert isinstance(obs, EditObservation)
    assert obs.focus_hit_at_1 is True
    assert obs.cluster_touch_rate == 1.0


# boundary
def test_boundary_empty_warnings() -> None:
    with patch("groundtruth.runtime.patch_auditor.audit_patch", return_value=_audit_return()):
        obs = kernel.observe_edit(_edit(["src/a.py"]), _run_state())
    assert obs.warnings == []


def test_boundary_warnings_coerced_to_str() -> None:
    """Auditor may emit non-str warnings; Boundary 1 forces str."""
    raw = _audit_return(warnings=[42, "tests_only_patch", None])
    with patch("groundtruth.runtime.patch_auditor.audit_patch", return_value=raw):
        obs = kernel.observe_edit(_edit(["src/a.py"]), _run_state())
    assert all(isinstance(w, str) for w in obs.warnings)
    assert "tests_only_patch" in obs.warnings


# adversarial -- the leakage cases
def test_adversarial_failing_test_names_dropped() -> None:
    """test_execution.failing_test_names must NOT cross Boundary 1."""
    raw = _audit_return(
        test_execution={
            "executed": True,
            "passed": 0,
            "failed": 1,
            "errored": 0,
            "all_passed": False,
            "failing_test_names": ["test_secret_pii_leak"],
        }
    )
    with patch("groundtruth.runtime.patch_auditor.audit_patch", return_value=raw):
        obs = kernel.observe_edit(_edit(["src/a.py"]), _run_state())
    assert "failing_test_names" not in obs.patch_shape["test_execution"]


def test_adversarial_error_traces_dropped() -> None:
    raw = _audit_return(
        test_execution={
            "executed": True,
            "passed": 0,
            "failed": 1,
            "errored": 0,
            "all_passed": False,
            "error_traces": ["Traceback (most recent call last):..."],
        }
    )
    with patch("groundtruth.runtime.patch_auditor.audit_patch", return_value=raw):
        obs = kernel.observe_edit(_edit(["src/a.py"]), _run_state())
    assert "error_traces" not in obs.patch_shape["test_execution"]


# mutation pin -- if focus_hit_at_1 default is changed from False to True,
# this test fails because it expects an explicit boolean inheritance.
def test_mutation_pin_focus_hit_default_false() -> None:
    raw = _audit_return(focus_hit_at_1=False, focus_hit_at_3=False, cluster_touch_rate=0.0)
    with patch("groundtruth.runtime.patch_auditor.audit_patch", return_value=raw):
        obs = kernel.observe_edit(_edit(["src/elsewhere.py"]), _run_state())
    assert obs.focus_hit_at_1 is False
    assert obs.focus_hit_at_3 is False
