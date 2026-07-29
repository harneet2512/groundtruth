"""MEASUREMENT DEFECT (CLAUDE.md §6, run 29904040782) — the no-patch taxonomy.

``gt_deep_metrics`` derived ``has_patch`` from the mini-swe trajectory's
``info.submission`` (or an OH ``git_patch``, or a log scrape). On a REAL submitted
DeepSWE run — ``D:/gt_runs/29948431988/wazero``, exit_status ``Submitted`` — that field
is the EMPTY STRING while ``jobs/**/artifacts/model.patch`` is 1,211 lines. The record
therefore said ``has_patch=false``; had the task not resolved it would have been labelled
``unresolved_no_patch_agent_ran`` == "the agent gave up", when the agent in fact shipped a
1,211-line patch that failed the hidden tests (a TASK-class failure).

These tests reproduce that artifact shape and pin the fix: the job's ``model.patch`` is the
authority, "no patch produced" and "patch produced, tests failed" stay DISTINCT enum
values, and the record says WHERE the patch fact came from.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
METRICS_PATH = ROOT / "scripts" / "swebench" / "gt_deep_metrics.py"


def _mod():
    spec = importlib.util.spec_from_file_location("gdm_patch_truth", METRICS_PATH)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TASK = "wazero-multi-module-snapshots"


def _pier_layout(tmp_path: Path, *, submission: str, patch_text: str | None) -> Path:
    """The exact pier layout: <results>/jobs/jobs/<ts>/<trial>/{agent,artifacts}."""
    trial = tmp_path / "jobs" / "jobs" / "2026-07-22__19-13-31" / f"{TASK}__GWAftfL"
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "info": {
                "exit_status": "Submitted",
                "submission": submission,
                "model_stats": {"api_calls": 41},
                "config": {"model": {"model_name": "deepseek-v4-flash"}},
            },
            "messages": [],
        }),
        encoding="utf-8",
    )
    if patch_text is not None:
        (trial / "artifacts").mkdir(parents=True)
        (trial / "artifacts" / "model.patch").write_text(patch_text, encoding="utf-8")
    return tmp_path


_BIG_PATCH = "diff --git a/x.go b/x.go\n--- a/x.go\n+++ b/x.go\n" + "+line\n" * 1207


def test_empty_submission_but_real_model_patch_is_not_a_no_patch_failure(tmp_path):
    """THE BUG. submission == '' + a 1,211-line model.patch."""
    rd = _pier_layout(tmp_path, submission="", patch_text=_BIG_PATCH)
    m = _mod()

    # the pre-fix inference path, verbatim, still yields False — this is the trap
    assert m._from_miniswe_trajectory(TASK, str(rd))["has_patch"] is False

    v = m.classify_outcome(TASK, "", {"action_count": 41, "resolved": None}, {},
                           "deepswe-miniswe", str(rd))
    assert v["has_patch"] is True
    assert v["outcome"] == "unresolved_with_patch"
    assert v["outcome"] != "unresolved_no_patch_agent_ran"
    assert v["patch_source"] == "model_patch_artifact"
    assert v["model_patch_lines"] == 1210
    assert v["model_patch_is_diff"] is True
    assert "tests failed" in v["failure_reason"]


def test_empty_model_patch_is_the_only_assertable_no_patch(tmp_path):
    """The artifact exists and is EMPTY: that — and only that — is 'produced nothing'."""
    rd = _pier_layout(tmp_path, submission="", patch_text="")
    v = _mod().classify_outcome(TASK, "", {"action_count": 41, "resolved": None}, {},
                                "deepswe-miniswe", str(rd))
    assert v["has_patch"] is False
    assert v["outcome"] == "unresolved_no_patch_agent_ran"
    assert v["patch_source"] == "model_patch_artifact"
    assert "EMPTY model.patch" in v["failure_reason"]


def test_missing_model_patch_artifact_is_flagged_unverified(tmp_path):
    """No artifact at all => the patch state is UNVERIFIED; never sold as fact."""
    rd = _pier_layout(tmp_path, submission="", patch_text=None)
    v = _mod().classify_outcome(TASK, "", {"action_count": 41, "resolved": None}, {},
                                "deepswe-miniswe", str(rd))
    assert v["patch_source"] != "model_patch_artifact"
    assert "UNVERIFIED" in v["failure_reason"]


def test_model_patch_beats_a_trajectory_that_claims_no_patch(tmp_path):
    """Artifact outranks inference even when traj explicitly says has_patch=False."""
    rd = _pier_layout(tmp_path, submission="", patch_text=_BIG_PATCH)
    v = _mod().classify_outcome(TASK, "", {"action_count": 41, "has_patch": False,
                                           "resolved": None}, {},
                                "deepswe-miniswe", str(rd))
    assert v["has_patch"] is True


def test_sibling_task_patch_is_never_borrowed(tmp_path):
    """A patch belonging to another task must not be attributed to this one."""
    other = tmp_path / "jobs" / "jobs" / "ts" / "someone-elses-task__ZZZ" / "artifacts"
    other.mkdir(parents=True)
    (other / "model.patch").write_text(_BIG_PATCH, encoding="utf-8")
    (tmp_path / "jobs" / "jobs" / "ts" / f"{TASK}__AAA" / "artifacts").mkdir(parents=True)
    (tmp_path / "jobs" / "jobs" / "ts" / f"{TASK}__AAA" / "artifacts"
     / "model.patch").write_text("", encoding="utf-8")
    facts = _mod()._model_patch_facts(TASK, str(tmp_path))
    assert facts["has_patch"] is False
    assert f"{TASK}__AAA" in facts["model_patch_path"]


def test_distinct_enum_values_never_fused():
    m = _mod()
    assert "unresolved_with_patch" in m.OUTCOMES
    assert "unresolved_no_patch_agent_ran" in m.OUTCOMES


def test_missing_cost_log_is_null_not_a_measured_zero(tmp_path):
    """SAME DEFECT CLASS: an absent [GT_COST] log used to emit $0.00 / 0 tokens."""
    out = _mod()._from_cost_log(str(tmp_path / "does_not_exist.log"))
    assert out["llm_cost_usd"] is None
    assert out["llm_tokens_in"] is None
    assert out["llm_calls"] is None
    empty = tmp_path / "trial_output.log"
    empty.write_text("no cost lines here\n", encoding="utf-8")
    assert _mod()._from_cost_log(str(empty))["llm_cost_usd"] is None


REAL_WAZERO = Path(r"D:/gt_runs/29948431988/wazero")


@pytest.mark.skipif(not REAL_WAZERO.is_dir(), reason="host artifact absent")
def test_real_wazero_artifact_grounds_the_fix():
    facts = _mod()._model_patch_facts("wazero-multi-module-snapshots", str(REAL_WAZERO))
    assert facts["has_patch"] is True
    assert facts["model_patch_lines"] and facts["model_patch_lines"] > 1000
