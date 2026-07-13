"""SS-3 defect-1 — INSTANCE-ID BINDING + report-authoritative INFRA gate.

Reproduces the run-29236533134 harness-truth defect: on the mini-swe-agent path the
pier ``result.json``/``instance_id`` is absent, so ``outcome.json`` /
``task_truth.json`` / ``reconciled_substrate_verdict.json`` came through
``instance_id=null`` and every task was auto-stamped
``failure_class=INFRA`` / ``INFRA_MISSING_ARTIFACT`` /
``in_resolved_denominator=false`` — false-darkening 3 genuine resolves (conan-17123
had reward=1, report resolved=true, a full trajectory).

The fixtures synthesize the conan-17123 shape (reward=1, report resolved=true, a jobs/
dir the pier glob misses). The reconciled verdict MUST be RESOLVED, not INFRA, and the
instance_id must be bound from the report key / matrix env / task dir name.

Each test carries a BITING MUTATION note (what break restores the INFRA stub).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"),
           os.path.join(_ROOT, "scripts", "swebench"),
           os.path.join(_ROOT, "scripts", "verify")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_TT_PATH = os.path.join(_ROOT, "scripts", "swebench", "task_truth.py")
_spec = importlib.util.spec_from_file_location("task_truth_ss3", _TT_PATH)
tt = importlib.util.module_from_spec(_spec)
sys.modules["task_truth_ss3"] = tt
_spec.loader.exec_module(tt)

IID = "conan-io__conan-17123"


def _make_conan_shape(root: str, *, with_trajectory: bool, resolved: bool = True,
                      reward_txt: str | None = "1") -> None:
    """Lay down the captured conan-17123 artifact shape at ``root``.

    - jobs/jobs/<ts>/<iid>__ll/verifier  → the pier glob ``jobs/*/*__*/result.json``
      MISSES (no result.json, no agent/ dir) → detect_infra_subtype = INFRA_MISSING.
    - report.json keyed by instance_id with a resolved verdict (the eval authority).
    - reward.txt at the task root (mini/CI bridge).
    - optionally the mini trajectory at the task root.
    """
    deep = os.path.join(root, "jobs", "jobs", "1783933917", f"{IID}__ll", "verifier")
    os.makedirs(deep, exist_ok=True)
    with open(os.path.join(root, "report.json"), "w", encoding="utf-8") as fh:
        json.dump({IID: {"resolved": resolved, "patch_successfully_applied": True,
                         "tests_status": {}}}, fh)
    if reward_txt is not None:
        with open(os.path.join(root, "reward.txt"), "w", encoding="utf-8") as fh:
            fh.write(reward_txt)
    if with_trajectory:
        traj = {"messages": [
            {"role": "user", "content": "Fix the issue."},
            {"role": "assistant", "content": "looking",
             "extra": {"actions": [{"command": "cat conan/x.py"}]}},
        ], "info": {"exit_status": "Submitted"}}
        with open(os.path.join(root, "mini-swe-agent.trajectory.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(traj, fh)


def _run(root: str, monkeypatch) -> dict:
    """Reproduce the Collect invocation: cwd = task root, jobs_dir = relative 'jobs'."""
    monkeypatch.chdir(root)
    monkeypatch.delenv("GT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("GT_MATRIX_TASK", raising=False)
    monkeypatch.delenv("GT_CERT_DIR", raising=False)
    return tt.build_task_truth("jobs")


# ---- report authority: no trajectory, report resolved → RESOLVED ---------- #
def test_reconciled_verdict_resolved_from_report_no_trajectory(tmp_path, monkeypatch):
    root = str(tmp_path / IID)
    _make_conan_shape(root, with_trajectory=False, resolved=True)
    truth = _run(root, monkeypatch)
    verdict = tt.build_reconciled_substrate_verdict(truth)

    assert truth["outcome"]["failure_class"] == "RESOLVED"
    assert truth["outcome"]["resolved"] is True
    assert truth["outcome"]["in_resolved_denominator"] is True
    assert truth["outcome"]["infra_subtype"] != "INFRA_MISSING_ARTIFACT"
    assert verdict["outcome_failure_class"] == "RESOLVED"
    assert verdict["in_resolved_denominator"] is True
    assert truth["instance_id"] == IID
    assert verdict["instance_id"] == IID
    # MUTATION 1: gate INFRA only on `len(_mini_turns) > 0` (drop the report-present
    #             clause) → this no-trajectory case re-stamps INFRA_MISSING_ARTIFACT.
    # MUTATION 2: drop the `report_resolved → reward` fill → reward stays None →
    #             classify_outcome never reaches RESOLVED (falls to GT/UNKNOWN).


# ---- full conan shape (report + reward + trajectory) → RESOLVED ----------- #
def test_reconciled_verdict_resolved_full_conan_shape(tmp_path, monkeypatch):
    root = str(tmp_path / IID)
    _make_conan_shape(root, with_trajectory=True, resolved=True)
    truth = _run(root, monkeypatch)
    verdict = tt.build_reconciled_substrate_verdict(truth)
    assert verdict["outcome_failure_class"] == "RESOLVED"
    assert truth["instance_id"] == IID
    assert truth["truth_reconciliation"] is not None
    assert truth["truth_reconciliation"]["reconciled"] is True


# ---- instance-id binding sources ------------------------------------------ #
def test_instance_id_from_report_key(tmp_path, monkeypatch):
    root = str(tmp_path / "unbound_dirname")  # dir name is NOT the iid
    _make_conan_shape(root, with_trajectory=False, resolved=False, reward_txt="0")
    truth = _run(root, monkeypatch)
    # bound from the report's sole key even when the dir name and env are unhelpful.
    assert truth["instance_id"] == IID
    # MUTATION: return None from instance_id_from_report → falls to dir name
    #           ("unbound_dirname"), breaking the pairing key.


def test_instance_id_from_matrix_env(tmp_path, monkeypatch):
    root = str(tmp_path / "jobs")  # dir name is the un-usable 'jobs' stem
    # no report at all → identity must come from the matrix env.
    (tmp_path / "jobs" / "jobs").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("GT_INSTANCE_ID", IID)
    monkeypatch.delenv("GT_MATRIX_TASK", raising=False)
    truth = tt.build_task_truth("jobs")
    assert truth["instance_id"] == IID


def test_instance_id_from_task_dir_name(tmp_path, monkeypatch):
    root = str(tmp_path / IID)
    (tmp_path / IID / "jobs" / "jobs").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("GT_MATRIX_TASK", raising=False)
    truth = tt.build_task_truth("jobs")
    assert truth["instance_id"] == IID


# ---- true INFRA preserved: BOTH report AND trajectory absent --------------- #
def test_true_infra_when_report_and_trajectory_both_absent(tmp_path, monkeypatch):
    root = str(tmp_path / "bridgecrewio__checkov-6893")
    (tmp_path / "bridgecrewio__checkov-6893" / "jobs" / "jobs").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("GT_MATRIX_TASK", raising=False)
    truth = tt.build_task_truth("jobs")
    # checkov-6893 was the ONE true INFRA (report.json absent, no trajectory).
    assert truth["outcome"]["failure_class"] == "INFRA"
    assert truth["outcome"]["infra_subtype"] == "INFRA_MISSING_ARTIFACT"
    assert truth["outcome"]["in_resolved_denominator"] is False


# ---- report resolved but reward.txt ABSENT → report fills the reward ------- #
def test_resolved_from_report_when_reward_txt_absent(tmp_path, monkeypatch):
    root = str(tmp_path / IID)
    # reward_txt=None → no reward.txt anywhere; the ONLY reward source is the report.
    _make_conan_shape(root, with_trajectory=False, resolved=True, reward_txt=None)
    truth = _run(root, monkeypatch)
    assert truth["outcome"]["failure_class"] == "RESOLVED"
    assert truth["outcome"]["reward"] == 1.0
    # MUTATION: drop the `report_resolved → reward` fill → reward stays None →
    #           classify_outcome cannot reach RESOLVED (this is the biting case).


# ---- report-authoritative UNRESOLVED is NOT a false RESOLVED --------------- #
def test_report_unresolved_is_not_resolved(tmp_path, monkeypatch):
    root = str(tmp_path / IID)
    _make_conan_shape(root, with_trajectory=True, resolved=False, reward_txt="0")
    truth = _run(root, monkeypatch)
    assert truth["outcome"]["failure_class"] != "RESOLVED"
    assert truth["outcome"]["infra_subtype"] != "INFRA_MISSING_ARTIFACT"
