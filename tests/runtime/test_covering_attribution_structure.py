"""Structured, producer-owned covering RED attribution evidence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from groundtruth.runtime import covering_runner as cr
from groundtruth.runtime import verification_plan as vp
from groundtruth.runtime.verification_plan import Check, VerificationPlan, run_plan


def _fake_base(monkeypatch, tmp_path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    monkeypatch.setattr(
        cr,
        "_make_base_worktree",
        lambda root, executor=None: (str(tmp_path / "base"), str(parent)),
    )
    monkeypatch.setattr(
        cr,
        "_cleanup_base_worktree",
        lambda root, wt, parent, executor=None: None,
    )


def test_trace_frame_attribution_preserves_exact_implicated_edit_and_inputs():
    current = {
        "verdict": "fail",
        "stdout_tail": (
            "tests/test_pay.py:12: in test_refund\n"
            "src/payment.py:88: in transform\n"
            "E   TypeError: unsupported operand type(s)"
        ),
        "stderr_tail": "",
    }

    result = cr.attribute_covering_red(
        current,
        {"src/unrelated.py", "src/payment.py"},
        test_files=["tests/test_pay.py"],
        repo_root="/unused",
        covering_files=["tests/test_pay.py", "tests/test_pay.py"],
    )

    assert result.attributed is True
    assert result.method == "trace_frame"
    assert result.current_verdict == "fail"
    assert result.base_verdict == "not_run"
    assert result.implicated_edited_paths == ("src/payment.py",)
    assert result.covering_files == ("tests/test_pay.py",)
    assert cr.is_red_attributable(
        current,
        {"src/unrelated.py", "src/payment.py"},
        test_files=["tests/test_pay.py"],
        repo_root="/unused",
        covering_files=["tests/test_pay.py"],
    ) is True
    with pytest.raises(FrozenInstanceError):
        result.attributed = False


def test_differential_attribution_preserves_base_verdict_and_sorted_input_sets(
    monkeypatch, tmp_path
):
    _fake_base(monkeypatch, tmp_path)
    current = {"verdict": "fail", "stdout_tail": "E   assert 1 == 2"}
    base_green = lambda cmd, cwd, timeout: (0, "1 passed", "")  # noqa: E731

    result = cr.attribute_covering_red(
        current,
        ["z.py", "a.py", "z.py"],
        test_files=["tests/test_z.py"],
        repo_root=str(tmp_path),
        covering_files=["tests/test_z.py", "tests/test_a.py", "tests/test_z.py"],
        executor=base_green,
    )

    assert result.attributed is True
    assert result.method == "differential"
    assert result.current_verdict == "fail"
    assert result.base_verdict == "pass"
    assert result.implicated_edited_paths == ("a.py", "z.py")
    assert result.covering_files == ("tests/test_a.py", "tests/test_z.py")


def test_differential_false_result_keeps_observed_base_red(monkeypatch, tmp_path):
    _fake_base(monkeypatch, tmp_path)
    current = {"verdict": "fail", "stdout_tail": "E   assert 1 == 2"}
    base_red = lambda cmd, cwd, timeout: (1, "1 failed", "")  # noqa: E731

    result = cr.attribute_covering_red(
        current,
        {"mod.py"},
        repo_root=str(tmp_path),
        covering_files=["test_mod.py"],
        executor=base_red,
    )

    assert result.attributed is False
    assert result.method == "differential"
    assert result.current_verdict == "fail"
    assert result.base_verdict == "fail"
    assert result.implicated_edited_paths == ("mod.py",)
    assert result.to_dict() == {
        "attributed": False,
        "method": "differential",
        "current_verdict": "fail",
        "base_verdict": "fail",
        "implicated_edited_paths": ["mod.py"],
        "covering_files": ["test_mod.py"],
    }


def test_frames_only_miss_is_structured_and_quiet():
    result = cr.attribute_covering_red(
        {"verdict": "fail", "stdout_tail": "E   assert 1 == 2"},
        {"mod.py"},
        covering_files=["test_mod.py"],
    )

    assert result.attributed is False
    assert result.method is None
    assert result.current_verdict == "fail"
    assert result.base_verdict == "not_run"
    assert result.implicated_edited_paths == ()
    assert result.covering_files == ("test_mod.py",)


def test_structured_attribution_requires_established_current_red():
    result = cr.attribute_covering_red(
        {"stdout_tail": "src/mod.py:3: in f\nE   TypeError: broken"},
        {"src/mod.py"},
        covering_files=["tests/test_mod.py"],
    )

    assert result.attributed is False
    assert result.method is None
    assert result.current_verdict == "unavailable"


def test_trace_frame_with_duplicate_edited_basenames_is_not_exactly_attributed():
    current = {
        "verdict": "fail",
        "stdout_tail": "payment.py:88: in transform\nE   TypeError: broken",
        "stderr_tail": "",
    }

    result = cr.attribute_covering_red(
        current,
        {"src/payment.py", "vendor/payment.py"},
        covering_files=["tests/test_pay.py"],
    )

    assert result.attributed is False
    assert result.method == "trace_frame"
    assert result.implicated_edited_paths == ()
    # The compatibility boolean retains the historical basename fallback. New
    # producer attestations and verification-plan telemetry use the exact result.
    assert cr.is_red_attributable(current, {"src/payment.py", "vendor/payment.py"}) is True


def test_single_basename_only_match_is_not_structured_path_proof():
    current = {
        "verdict": "fail",
        "stdout_tail": "payment.py:88: in transform\nE   TypeError: broken",
    }

    result = cr.attribute_covering_red(current, {"src/payment.py"})

    assert result.attributed is False
    assert result.implicated_edited_paths == ()
    assert cr.is_red_attributable(current, {"src/payment.py"}) is True


def test_absolute_frame_is_canonicalized_relative_to_repo_root():
    current = {
        "verdict": "fail",
        "stdout_tail": "/repo/src/payment.py:88: in transform\nE   TypeError: broken",
    }

    result = cr.attribute_covering_red(
        current,
        {"src/payment.py"},
        repo_root="/repo",
        covering_files=["tests/test_payment.py"],
    )

    assert result.attributed is True
    assert result.implicated_edited_paths == ("src/payment.py",)


def test_verification_plan_carries_structured_attribution_without_replacing_runner_detail(
    monkeypatch,
):
    check = Check(
        kind="unit",
        command=("pytest", "tests/test_mod.py"),
        selection_basis="fact_covering",
        covered_entities=("edited",),
        attribution_requirement="edit_attributed",
        targets=("tests/test_mod.py",),
    )
    plan = VerificationPlan(
        patch_revision="P1",
        graph_revision="G1",
        changed_entities=("edited",),
        obligations=(),
        checks=(check,),
        edited_files=("mod.py",),
    )
    runner_detail = {"verdict": "fail", "executed": True, "exit_code": 1}
    evidence = cr.CoveringAttribution(
        attributed=True,
        method="differential",
        current_verdict="fail",
        base_verdict="pass",
        implicated_edited_paths=("mod.py",),
        covering_files=("tests/test_mod.py",),
    )
    monkeypatch.setattr(vp, "run_covering_tests", lambda *a, **k: runner_detail)
    monkeypatch.setattr(vp, "attribute_covering_red", lambda *a, **k: evidence)

    result = run_plan(plan, repo_root="/repo")[0]

    assert result.attribution_satisfied is True
    assert result.detail["exit_code"] == 1
    assert result.detail["attribution"] == evidence.to_dict()
