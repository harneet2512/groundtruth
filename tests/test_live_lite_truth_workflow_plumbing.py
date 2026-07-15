"""Pins Live-Lite post-run truth and summarize plumbing.

These tests cover the real mini-swe-agent artifact layout: a task-scoped root,
not the historical pier ``jobs/<run>/<task>/agent`` layout.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
TASK_TRUTH = ROOT / "scripts" / "swebench" / "task_truth.py"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(job: str, name: str) -> dict:
    return next(
        step
        for step in _workflow()["jobs"][job]["steps"]
        if step.get("name") == name
    )


def _task_truth_module():
    spec = importlib.util.spec_from_file_location("live_lite_task_truth", TASK_TRUTH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarize_exposes_repo_python_modules() -> None:
    env = _step("summarize", "Build canonical PERF and exact-128 diagnosis").get("env") or {}
    assert env.get("PYTHONPATH") == (
        "${{ github.workspace }}/src:"
        "${{ github.workspace }}/scripts/swebench:"
        "${{ github.workspace }}/scripts/metrics"
    )


def test_collect_finalizes_truth_from_explicit_task_root() -> None:
    run = _step("trial", "Collect results")["run"]
    root_assignment = 'TASK_ARTIFACT_ROOT="/tmp/gt/${{ matrix.task }}"'
    trajectory_bridge = (
        'cp /tmp/gt_out/mini-swe-agent.trajectory.json '
        '"$TASK_ARTIFACT_ROOT/mini-swe-agent.trajectory.json"'
    )
    initial_truth = "TASK_TRUTH_INITIAL"
    deep_metrics = "scripts/swebench/gt_deep_metrics.py"
    final_truth = "TASK_TRUTH_FINAL"

    assert root_assignment in run
    assert trajectory_bridge in run
    assert run.count("scripts/swebench/task_truth.py") == 2
    assert run.count('scripts/swebench/task_truth.py "$TASK_ARTIFACT_ROOT"') == 2
    assert run.index(root_assignment) < run.index(trajectory_bridge)
    assert run.index(trajectory_bridge) < run.index(initial_truth)
    assert run.index(initial_truth) < run.index(deep_metrics)
    assert run.index(deep_metrics) < run.index(final_truth)
    assert run.index(final_truth) < run.index("GT_METRICS_COMPLETE")
    assert 'cp "$TASK_ARTIFACT_ROOT/task_truth.json" trial_results/task_truth.json' in run
    assert (
        'cp "$TASK_ARTIFACT_ROOT/reconciled_substrate_verdict.json" '
        'trial_results/reconciled_substrate_verdict.json'
    ) in run


def test_collect_uploads_the_exact_task_scoped_runtime_ledger() -> None:
    run = _step("trial", "Collect results")["run"]
    root_copy = (
        'cp /tmp/gt_out/gt_runtime_ledger.jsonl "$_GT_LEDGER_DEST"'
    )
    uploaded_copy = (
        'cp "$_GT_LEDGER_DEST" '
        '"trial_results/gt_runtime_ledger_${{ matrix.task }}.jsonl"'
    )
    completion = 'artifact_paths = {'

    assert root_copy in run
    assert uploaded_copy in run
    assert run.index(root_copy) < run.index(uploaded_copy) < run.index(completion)


def test_collect_rebinds_attestation_to_the_task_scoped_ledger_name() -> None:
    run = _step("trial", "Collect results")["run"]
    source_validation = "validate_attestation(source_ledger, source_attestation)"
    regenerated = "write_attestation("
    renamed_validation = (
        'os.environ["GT_LEDGER_DEST"], os.environ["GT_ATTEST_DEST"]'
    )
    uploaded = (
        'cp "$_GT_ATTEST_DEST" '
        '"trial_results/gt_runtime_ledger_attestation_${{ matrix.task }}.json"'
    )

    assert source_validation in run
    assert regenerated in run
    assert renamed_validation in run
    assert uploaded in run
    assert run.index(source_validation) < run.index(regenerated) < run.index(uploaded)


def test_metrics_gate_requires_final_parsed_task_truth() -> None:
    run = _step("trial", "Collect results")["run"]
    gate = run[run.index('_MM_MISSING=""'):]
    final = run[run.index("# TASK_TRUTH_FINAL:"):run.index('_MM_MISSING=""')]

    assert 'rm -f "$TASK_ARTIFACT_ROOT/task_truth.json" trial_results/task_truth.json' in final
    assert '[ -s "trial_results/task_truth.json" ]' in gate
    assert 'd.get("schema")=="gt.task_truth.v1"' in gate
    assert 't.get("turns_observed")>=0' in gate
    assert 'int(i.get("mini_bytes") or 0)>0' in gate
    assert "task_truth_trajectory_integrity" in gate


def test_summarize_requires_completion_receipt_for_every_expected_task() -> None:
    step = _step("summarize", "Build canonical PERF and exact-128 diagnosis")
    run = step["run"]

    assert step["env"]["GT_TRIAL_RESULT"] == "${{ needs.trial.result }}"
    assert "gt_task_completion.json" in run
    assert 'receipt.get("schema") != "gt.task_completion.v1"' in run
    assert 'receipt.get("task") != task' in run
    assert 'receipt.get("workflow_run_id") != os.environ["GT_RUN_ID"]' in run
    assert "GT_FEATURE_POPULATION_INVALID" in run
    assert "GT_TRIAL_POPULATION_RC" in run
    assert 'GT_TRIAL_RESULT" != "success' in run


def test_completion_receipt_binds_every_live_verdict_input() -> None:
    run = _step("trial", "Collect results")["run"]
    summarize = _step("summarize", "Build canonical PERF and exact-128 diagnosis")["run"]
    receipt_writer = run[run.index("artifact_paths = {"):run.index("document = {")]
    receipt_reader = summarize[
        summarize.index("expected_hashes = {"):
        summarize.index("if not isinstance(hashes, dict)")
    ]

    # These are the raw, task-local authorities consumed by the live diagnosis
    # and ACQ provenance join. Sealing only their derived summaries permits a
    # post-completion artifact substitution to change the SS verdict silently.
    required = {
        "mini-swe-agent.trajectory.json",
        "brief_result.json",
        "gt_artifacts/gt_run_identity.json",
    }
    for relative in required:
        literal = f'"{relative}"'
        assert literal in receipt_writer
        assert literal in receipt_reader

    assert (
        '"mini-swe-agent.trajectory.json": '
        'out.parent / "mini-swe-agent.trajectory.json"'
    ) in receipt_writer
    assert '"brief_result.json": out.parent / "brief_result.json"' in receipt_writer
    assert (
        '"gt_artifacts/gt_run_identity.json": '
        'out.parent / "gt_artifacts/gt_run_identity.json"'
    ) in receipt_writer
    assert 'trajectory = load("mini-swe-agent.trajectory.json")' in run
    assert 'identity = load("gt_artifacts/gt_run_identity.json")' in run
    assert 'brief = load("brief_result.json")' in run
    assert 'identity.get("workflow_run_id") != os.environ.get("GITHUB_RUN_ID", "")' in run
    assert "GT_TASK_COMPLETION_INVALID:trajectory" in run
    assert "GT_TASK_COMPLETION_INVALID:brief_result" in run
    assert "GT_TASK_COMPLETION_INVALID:run_identity" in run
    assert '"mini-swe-agent.trajectory.json": root / "mini-swe-agent.trajectory.json"' in run
    assert '"brief_result.json": Path("/tmp/gt/brief_result.json")' in run
    assert (
        '"gt_artifacts/gt_run_identity.json": '
        'Path("/tmp/gt_out/gt_run_identity.json")'
    ) in run
    assert "GT_TASK_COMPLETION_INVALID:source_parity:{name}" in run
    assert 'brief.get("brief_sha256") != hashlib.sha256(' in run
    assert "os.fsync(stream.fileno())" in run
    assert "os.replace(temporary, out)" in run


def test_explicit_task_root_truth_observes_agent_and_final_deep_metrics(
    tmp_path: Path, monkeypatch,
) -> None:
    task = "conan-io__conan-17514"
    task_root = tmp_path / task
    task_root.mkdir()
    trajectory = {
        "info": {
            "model_stats": {"api_calls": 41},
            "exit_status": "Submitted",
            "submission": (
                "diff --git a/conan/api/subapi/config.py "
                "b/conan/api/subapi/config.py\n"
                "--- a/conan/api/subapi/config.py\n"
                "+++ b/conan/api/subapi/config.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
            ),
        },
        "messages": [
            {
                "role": "assistant", "content": "inspect", "tool_calls": [{
                    "id": "inspect-1", "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": "sed -n '1,20p' conan.py"}),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "inspect-1", "content": "done"},
        ],
    }
    (task_root / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(trajectory), encoding="utf-8"
    )
    (task_root / "reward.txt").write_text("0\n", encoding="utf-8")
    (task_root / "report.json").write_text(
        json.dumps({task: {"resolved": False}}), encoding="utf-8"
    )
    deep_path = task_root / f"gt_deep_metrics_{task}.json"
    deep_path.write_text(
        json.dumps(
            {
                "outcome": "unresolved_with_patch",
                "resolved": False,
                "gt_blocks_delivered": 3,
                "gt_blocks_consumed": 1,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("GT_INSTANCE_ID", task)
    monkeypatch.setenv("GT_MATRIX_TASK", task)
    monkeypatch.chdir(tmp_path)
    truth = _task_truth_module().build_task_truth(str(task_root), instance_id=task)

    assert truth["signals"]["n_agent_steps"] == 41
    assert truth["outcome"]["failure_class"] not in {"INFRA", "UNKNOWN"}
    assert truth["outcome"]["in_resolved_denominator"] is True
    assert truth["trajectory_integrity"]["mini_bytes"] > 0
    assert truth["patch_hygiene"]["classification"] == "source_fix"
    assert truth["deep_metrics"]["path"] == str(deep_path)
    assert truth["deep_metrics"]["outcome"] == "unresolved_with_patch"
    assert truth["deep_metrics"]["gt_blocks_delivered"] == 3
