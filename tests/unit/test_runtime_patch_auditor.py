from __future__ import annotations

import sys

from groundtruth.runtime.patch_auditor import ROOT_SCAFFOLD_PATTERNS, audit_patch
from groundtruth.runtime.hook_truth import normalize_hook_truth
from groundtruth.runtime.project_memory import build_project_memory
from groundtruth.runtime.replan import evaluate_replan_triggers
from groundtruth.runtime.report import build_benchmark_report
from groundtruth.runtime.telemetry import append_block, read_blocks
from groundtruth.runtime.test_runner import (
    execute_test_command,
    select_test_command,
)


def test_patch_auditor_flags_root_scaffold_and_tests_only() -> None:
    result = audit_patch(
        ".",
        plan={"cluster_files": ["pkg/core.py"], "expected_side_files": ["tests/test_core.py"]},
        name_status=[("A", "final_verification.py"), ("A", "tests/test_core.py")],
    )

    assert result["root_scaffold_files_added"] == ["final_verification.py"]
    assert result["tests_only_patch"] is True
    assert result["expected_side_files_missing"] == []
    assert result["recommendation"] == "likely_invalid"


def test_patch_auditor_reports_cluster_touch_rate_and_expected_missing() -> None:
    result = audit_patch(
        ".",
        plan={"cluster_files": ["pkg/core.py"], "expected_side_files": ["tests/test_core.py"]},
        name_status=[("M", "pkg/core.py"), ("M", "pkg/other.py")],
    )

    assert result["source_files_touched"] == ["pkg/core.py", "pkg/other.py"]
    assert result["cluster_files_touched"] == ["pkg/core.py"]
    assert result["outside_cluster_files"] == ["pkg/other.py"]
    assert result["cluster_touch_rate"] == 0.5
    assert result["expected_side_files_missing"] == ["tests/test_core.py"]
    assert result["recommendation"] == "needs_replan"


def test_patch_auditor_reports_focus_adherence_metrics() -> None:
    result = audit_patch(
        ".",
        plan={
            "cluster_files": ["pkg/core.py", "pkg/other.py"],
            "agent_focus_files": [
                {"file": "pkg/core.py"},
                {"file": "pkg/other.py"},
                {"file": "pkg/third.py"},
            ],
        },
        name_status=[("M", "pkg/core.py"), ("M", "docs/notes.md")],
    )

    assert result["agent_focus_files_touched"] == ["pkg/core.py"]
    assert result["edited_ranked_focus_files"] == ["pkg/core.py"]
    assert result["brief_edit_overlap"] == 0.3333
    assert result["focus_hit_at_1"] is True
    assert result["focus_hit_at_3"] is True
    assert result["focus_edit_precision"] == 0.5


def test_patch_auditor_can_distinguish_cluster_touch_from_focus_miss() -> None:
    result = audit_patch(
        ".",
        plan={
            "cluster_files": ["pkg/core.py", "pkg/other.py"],
            "agent_focus_files": [{"file": "pkg/core.py"}],
        },
        name_status=[("M", "pkg/other.py")],
    )

    assert result["cluster_files_touched"] == ["pkg/other.py"]
    assert result["cluster_touch_rate"] == 1.0
    assert result["agent_focus_files_touched"] == []
    assert result["focus_hit_at_1"] is False
    assert "no_agent_focus_file_touched" in result["warnings"]


def test_replan_thresholds() -> None:
    result = evaluate_replan_triggers(
        edited_files=[
            "final_check.py",
            "docs/a.md",
            "docs/b.md",
            "docs/c.md",
            "docs/d.md",
        ],
        plan={"cluster_files": ["pkg/core.py"]},
        warning_history=["edits_outside_candidate_cluster", "edits_outside_candidate_cluster"],
    )

    assert result["should_replan"] is True
    assert "first_edit_root_scaffold" in result["reasons"]
    assert "three_edits_outside_cluster" in result["reasons"]
    assert "no_cluster_file_after_five_edits" in result["reasons"]
    assert result["replan_stage"] == "recompute"


def test_replan_reports_missing_plan_cluster() -> None:
    result = evaluate_replan_triggers(
        edited_files=["pkg/core.py"],
        plan={},
        warning_history=[],
    )

    assert result["should_replan"] is True
    assert result["reasons"] == ["missing_or_empty_plan_cluster"]


def test_replan_triggers_on_focus_drift_before_cluster_drift() -> None:
    result = evaluate_replan_triggers(
        edited_files=["pkg/nearby.py"],
        plan={
            "cluster_files": ["pkg/core.py", "pkg/nearby.py"],
            "agent_focus_files": [{"file": "pkg/core.py"}],
        },
        warning_history=[],
    )

    assert result["should_replan"] is True
    assert "first_edit_missed_focus" in result["reasons"]
    assert result["replan_stage"] == "corrective"
    assert result["agent_focus_files"] == ["pkg/core.py"]
    assert result["next_actions"] == ["Open and edit ranked focus file first: pkg/core.py."]


def test_replan_compacts_validation_failures_into_next_actions() -> None:
    result = evaluate_replan_triggers(
        edited_files=["pkg/core.py"],
        plan={
            "cluster_files": ["pkg/core.py"],
            "agent_focus_files": [{"file": "pkg/core.py"}],
        },
        test_result={
            "executed": True,
            "all_passed": False,
            "failing_test_names": ["tests/test_core.py::test_contract"],
        },
        patch_shape={"expected_side_files_missing": ["tests/test_core.py"]},
    )

    assert result["should_replan"] is True
    assert result["validation_failures"] == ["tests/test_core.py::test_contract"]
    assert "failing_tests_after_edit" in result["reasons"]
    assert "expected_side_files_missing" in result["reasons"]
    assert result["next_actions"] == [
        "Use visible failing test evidence first: tests/test_core.py::test_contract.",
        "Review expected side file(s): tests/test_core.py.",
    ]


def test_select_test_command_prefers_contract_tests(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    result = select_test_command(
        str(tmp_path),
        mode="contract",
        plan={"expected_side_files": ["tests/test_core.py"]},
    )

    assert result["command"] == ["pytest", "tests/test_core.py"]
    assert result["reason"] == "contract_tests"


def test_select_test_command_accepts_dict_expected_side_files(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    result = select_test_command(
        str(tmp_path),
        mode="contract",
        plan={"expected_side_files": [{"path": "tests/test_core.py", "kind": "contract_test"}]},
    )

    assert result["command"] == ["pytest", "tests/test_core.py"]


def test_runtime_telemetry_writer_emits_stable_block(tmp_path) -> None:
    path = append_block(
        "gt_patch_shape",
        {"warnings": ["tests_only_patch"]},
        log_dir=str(tmp_path),
        task_id="task-1",
    )

    assert path is not None
    records = read_blocks(path)
    assert records[-1]["block"] == "gt_patch_shape"
    assert records[-1]["gt_patch_shape"]["warnings"] == ["tests_only_patch"]


def test_project_memory_detects_layout_and_side_rules(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "CHANGELOG.md").write_text("# changes\n", encoding="utf-8")
    (tmp_path / "py.typed").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    memory = build_project_memory(str(tmp_path))

    assert memory["package_manager"] == "pytest"
    assert memory["test_layout"] == ["tests"]
    assert memory["changelog_convention"] == "CHANGELOG.md"
    assert {"kind": "public_api_change", "side_file": "changelog"} in memory[
        "common_side_file_rules"
    ]


def test_root_scaffold_patterns_cover_multiple_languages() -> None:
    """Fix #3: scaffold detection generalises beyond Python."""
    languages_to_check = {
        "go": "main_test.go",
        "javascript": "foo.spec.js",
        "typescript": "feature.test.ts",
        "rust": "repro_bug.rs",
        "java": "MyTest.java",
        "ruby": "user_spec.rb",
    }
    import fnmatch

    for lang, sample in languages_to_check.items():
        matched = any(fnmatch.fnmatch(sample, pat) for pat in ROOT_SCAFFOLD_PATTERNS)
        assert matched, f"{lang} sample {sample} does not match any scaffold pattern"


def test_audit_patch_flags_root_scaffold_in_go_repo() -> None:
    """Fix #3: a go repro file at root is flagged as scaffold."""
    result = audit_patch(
        ".",
        plan={"cluster_files": ["pkg/auth.go"]},
        name_status=[("A", "repro_auth.go"), ("M", "pkg/auth.go")],
    )
    assert "repro_auth.go" in result["root_scaffold_files_added"]
    assert "pkg/auth.go" in result["cluster_files_touched"]


def test_audit_patch_flags_root_scaffold_in_js_repo() -> None:
    """Fix #3: a .test.js file at root is flagged as scaffold."""
    result = audit_patch(
        ".",
        plan={"cluster_files": ["src/auth.ts"]},
        name_status=[("A", "auth.spec.ts"), ("M", "src/auth.ts")],
    )
    assert "auth.spec.ts" in result["root_scaffold_files_added"]


def test_execute_test_command_no_command_returns_not_executed(tmp_path) -> None:
    """Fix #1: empty command returns executed=False without spawning."""
    result = execute_test_command(str(tmp_path), [])
    assert result["executed"] is False
    assert result["all_passed"] is False
    assert result["reason"] == "no_command"


def test_execute_test_command_runs_python_oneliner(tmp_path) -> None:
    """Fix #1: subprocess actually runs and a clean exit gives all_passed=True."""
    result = execute_test_command(
        str(tmp_path),
        [sys.executable, "-c", "print('1 passed in 0.01s')"],
        timeout_seconds=10,
    )
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["all_passed"] is True


def test_execute_test_command_failing_python_oneliner_marks_failed(tmp_path) -> None:
    """Fix #1: nonzero exit + parsed failure count flips all_passed=False."""
    result = execute_test_command(
        str(tmp_path),
        [
            sys.executable,
            "-c",
            "import sys; print('1 failed, 0 passed'); sys.exit(1)",
        ],
        timeout_seconds=10,
    )
    assert result["executed"] is True
    assert result["exit_code"] == 1
    assert result["all_passed"] is False


def test_execute_test_command_writes_telemetry_block(tmp_path) -> None:
    """Fix #1: gt_test_execution telemetry block is appended when log_dir set."""
    log_dir = tmp_path / "logs"
    execute_test_command(
        str(tmp_path),
        [sys.executable, "-c", "print('ok')"],
        timeout_seconds=10,
        log_dir=str(log_dir),
        task_id="task-exec-1",
    )
    records = read_blocks(log_dir / "gt_runtime_telemetry.jsonl")
    assert any(rec.get("block") == "gt_test_execution" for rec in records)
    validation = [rec for rec in records if rec.get("block") == "gt_test_validation"]
    assert validation
    assert validation[-1]["gt_test_validation"]["exit_code"] == 0


def test_audit_patch_consumes_test_failure_into_warning() -> None:
    """Fix #1: audit_patch surfaces failing_tests warning when tests failed."""
    result = audit_patch(
        ".",
        plan={"cluster_files": ["pkg/core.py"]},
        name_status=[("M", "pkg/core.py")],
        test_result={
            "executed": True,
            "all_passed": False,
            "passed": 1,
            "failed": 2,
            "errored": 0,
        },
    )
    assert "failing_tests" in result["warnings"]
    assert result["recommendation"] == "needs_replan"
    assert result["test_execution"]["executed"] is True
    assert result["test_execution"]["failed"] == 2


def test_audit_patch_test_passing_yields_on_plan() -> None:
    """Fix #1: passing tests with otherwise-clean diff stays on_plan."""
    result = audit_patch(
        ".",
        plan={"cluster_files": ["pkg/core.py"]},
        name_status=[("M", "pkg/core.py")],
        test_result={
            "executed": True,
            "all_passed": True,
            "passed": 5,
            "failed": 0,
            "errored": 0,
        },
    )
    assert "failing_tests" not in result["warnings"]
    assert result["recommendation"] == "on_plan"
    assert result["test_execution"]["all_passed"] is True


def test_replan_triggers_on_failing_tests() -> None:
    """Fix #1: failing test result triggers a replan."""
    decision = evaluate_replan_triggers(
        edited_files=["pkg/core.py"],
        plan={"cluster_files": ["pkg/core.py"]},
        warning_history=[],
        test_result={"executed": True, "all_passed": False, "failed": 1},
    )
    assert decision["should_replan"] is True
    assert "failing_tests_after_edit" in decision["reasons"]
    assert "Selected contract tests are failing" in decision["corrective_instruction"]


def test_replan_no_trigger_when_tests_pass() -> None:
    """Fix #1: passing tests do not by themselves trigger replan."""
    decision = evaluate_replan_triggers(
        edited_files=["pkg/core.py"],
        plan={"cluster_files": ["pkg/core.py"]},
        warning_history=[],
        test_result={"executed": True, "all_passed": True, "failed": 0},
    )
    assert decision["should_replan"] is False
    assert "failing_tests_after_edit" not in decision["reasons"]


def test_benchmark_report_aggregates_runtime_blocks(tmp_path) -> None:
    task = tmp_path / "task_1"
    task.mkdir()
    append_block(
        "gt_patch_shape",
        {
            "source_files_touched": ["pkg/core.py"],
            "test_files_touched": [],
            "root_scaffold_files_added": [],
            "empty_patch": False,
            "cluster_touch_rate": 1.0,
            "brief_edit_overlap": 1.0,
            "focus_hit_at_1": True,
            "focus_hit_at_3": True,
            "focus_edit_precision": 1.0,
            "warnings": [],
        },
        log_dir=str(task),
        task_id="task_1",
    )
    (task / "task_1_v7_brief.jsonl").write_text(
        '{"module_7_contract":{"contract_lines":["assert x"]},'
        '"module_7_cochange":{"cluster_files":[{"file":"pkg/core.py"}]}}\n',
        encoding="utf-8",
    )

    report = build_benchmark_report(str(tmp_path))

    assert report["task_count"] == 1
    assert report["source_touch_rate"] == 1.0
    assert report["focus_hit_at_1_rate"] == 1.0
    assert report["focus_hit_at_3_rate"] == 1.0
    assert report["brief_edit_overlap"] == 1.0
    assert report["contract_extraction_rate"] == 1.0
    assert report["cochange_cluster_rate"] == 1.0
    assert report["runtime_warning_count"] == 0


def test_hook_truth_normalizes_legacy_visible_output() -> None:
    truth = normalize_hook_truth(
        {
            "hook": "post_edit",
            "endpoint": "verify",
            "output": "<gt-evidence>\n[GT_RUNTIME] warning\n</gt-evidence>",
        }
    )

    assert truth == {
        "hook_logged": True,
        "hook_visible_to_agent": True,
        "hook_blocked": False,
        "final_audit_only": False,
    }


def test_benchmark_report_tracks_hook_truthfulness(tmp_path) -> None:
    task = tmp_path / "task_hook"
    task.mkdir()
    (task / "gt_hook_telemetry.jsonl").write_text(
        '{"hook":"post_edit","endpoint":"verify","output":"<gt-evidence>visible</gt-evidence>",'
        '"gt_runtime":{"runtime_warnings":["root_scaffold"]}}\n',
        encoding="utf-8",
    )

    report = build_benchmark_report(str(tmp_path))

    assert report["hook_logged_rate"] == 1.0
    assert report["hook_visible_to_agent_rate"] == 1.0
    assert report["hook_blocked_rate"] == 0.0
    row = report["tasks"][0]
    assert row["hook_logged"] is True
    assert row["hook_visible_to_agent"] is True
