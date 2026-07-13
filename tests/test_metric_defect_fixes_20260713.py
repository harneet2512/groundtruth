"""Fixture-pinned regressions for the three metric defects the orchestrator confirmed:

  1. task_truth.json / outcome.json INFRA stub on a HEALTHY mini run
     (fixture: run 29217805592 / facebookresearch__hydra-3005).
  2. first_edit_action=0 DETECTION ARTIFACT (not-detected must be null, never 0)
     (fixture: deepmetrics_30/_work_oldgt/amoffat__sh-744, old-shape trajectory).
  3. brief_delivered=0 on the mini shape — source the count from W1's v2 consumption
     ledger brief channel (any closed brief.* block), not the <gt-task-brief> tag.

Each test carries a BITING MUTATION comment. Fixture-dependent tests SKIP when the local
run data is absent (so CI without D:/gt_runs still passes); the synthetic halves always run.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "scripts", "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gt_deep_metrics as dm  # noqa: E402
import consumption_ledger as cl  # noqa: E402
import task_truth as tt  # noqa: E402

_RUN = r"D:\gt_runs\29217805592\art"
_HYDRA = os.path.join(_RUN, "facebookresearch__hydra-3005")
_OLDGT = r"D:\gt_runs\deepmetrics_30\_work_oldgt\amoffat__sh-744"
_HAYSTACK_TRAJ = os.path.join(_RUN, "deepset-ai__haystack-8489", "mini-swe-agent.trajectory.json")


# =========================================================================== #
# Defect 1 — task_truth must NEVER report INFRA when a full-run trajectory is present.
# =========================================================================== #
@pytest.mark.skipif(not os.path.isfile(os.path.join(_HYDRA, "mini-swe-agent.trajectory.json")),
                    reason="hydra-3005 fixture not present locally")
def test_task_truth_no_infra_stub_on_healthy_run(monkeypatch):
    # Reproduce the real Collect invocation: cwd = task dir, jobs_dir = relative "jobs".
    monkeypatch.chdir(_HYDRA)
    truth = tt.build_task_truth("jobs")
    outcome = truth["outcome"]
    # the mislabel is gone: a present 153-turn trajectory disproves INFRA_MISSING_ARTIFACT.
    assert outcome["infra_subtype"] != "INFRA_MISSING_ARTIFACT"
    assert outcome["failure_class"] != "INFRA"
    assert truth["truth_reconciliation"] is not None
    assert truth["truth_reconciliation"]["reconciled"] is True
    assert truth["trajectory_state"]["turns_observed"] > 0
    assert truth["patch_hygiene"]["classification"] != "missing_model_patch"
    # MUTATION: removing _augment_artifacts_root or the reconcile guard restores the INFRA
    # stub (turns_observed=0, infra_subtype=INFRA_MISSING_ARTIFACT).


def test_task_truth_root_augment_finds_mini_trajectory(tmp_path):
    # synthetic: a mini trajectory at the task root + an empty jobs/ dir (pier glob misses).
    (tmp_path / "jobs").mkdir()
    traj = {"messages": [{"role": "assistant", "content": "x",
                          "tool_calls": [{"function": {"arguments": json.dumps({"command": "ls"})}}]}],
            "info": {"exit_status": "Submitted"}}
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    arts = tt._augment_artifacts_root(str(tmp_path / "jobs"),
                                      {"mini_trajectory": None, "trial_dir": None})
    assert arts["mini_trajectory"] is not None
    assert arts["mini_trajectory"].endswith("mini-swe-agent.trajectory.json")
    # MUTATION: an augment that only searched the jobs glob would leave mini_trajectory None.


# =========================================================================== #
# Defect 2 — first_edit_action not-detected must be None, never 0.
# =========================================================================== #
def test_first_edit_action_none_when_no_edit_detected(tmp_path):
    # a mini trajectory with only a view (no edit) → first_edit_action must be None.
    traj = {"messages": [
        {"role": "assistant", "content": "look",
         "tool_calls": [{"function": {"arguments": json.dumps({"command": "cat a.py"})}}]},
        {"role": "tool", "content": "file contents"},
    ], "info": {"exit_status": "Submitted"}}
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    res = dm._from_miniswe_trajectory("t", str(tmp_path))
    assert res["found"] is True
    assert res["edits"] == 0
    assert res["first_edit_action"] is None  # NOT 0
    # MUTATION: the old sentinel 0 would make this 0 and poison first-edit deltas.


def test_first_edit_action_int_when_edit_detected(tmp_path):
    traj = {"messages": [
        {"role": "assistant", "content": "look",
         "tool_calls": [{"function": {"arguments": json.dumps({"command": "cat a.py"})}}]},
        {"role": "tool", "content": "x"},
        {"role": "assistant", "content": "edit",
         "tool_calls": [{"function": {"arguments": json.dumps(
             {"command": "sed -i s/x/y/ a.py"})}}]},  # mini edits are bash commands
        {"role": "tool", "content": "ok"},
    ], "info": {"exit_status": "Submitted"}}
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    res = dm._from_miniswe_trajectory("t", str(tmp_path))
    assert isinstance(res["first_edit_action"], int) and res["first_edit_action"] >= 1


@pytest.mark.skipif(not os.path.isfile(os.path.join(_OLDGT, "mini-swe-agent.trajectory.json")),
                    reason="old-shape sh-744 fixture not present locally")
def test_first_edit_action_none_on_oldshape_fixture():
    res = dm._from_miniswe_trajectory("amoffat__sh-744", _OLDGT)
    # the old-shape trajectory's edits are not detected by the mini parser → None, not 0.
    if res.get("edits") == 0:
        assert res["first_edit_action"] is None
    # MUTATION: sentinel 0 would report first_edit_action=0 for this undetected-edit run.


# =========================================================================== #
# Defect 3 — brief delivery sourced from the v2 brief channel (any closed brief.* block),
# not the legacy <gt-task-brief> tag.
# =========================================================================== #
def _brief_channel_count(trajectory_or_path) -> int:
    led = (cl.ledger_from_trajectory_path(trajectory_or_path)
           if isinstance(trajectory_or_path, str)
           else cl.build_consumption_ledger(trajectory_or_path))
    return len({
        e.get("msg_index") for e in led.get("entries", [])
        if e.get("source") == "trajectory" and e.get("delivery_channel") == "brief"
        and e.get("msg_index") is not None
    })


def test_brief_delivered_counts_closed_bundle_not_task_brief_tag():
    # a brief bundle whose block is <gt-obligations> (NOT <gt-task-brief>) in the m1 user msg:
    # the legacy tag counter reports 0, the v2 brief channel reports 1.
    user = 'solve this <gt-obligations file="x.py">- [ ] fix the bug</gt-obligations>'
    syn = {"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"arguments": json.dumps({"command": "ls"})}}]},
    ]}
    assert user.count("<gt-task-brief") == 0          # legacy counter would say brief_delivered=0
    assert _brief_channel_count(syn) == 1             # the fix's source says 1
    # MUTATION: keeping content.count("<gt-task-brief") as the source would report 0 here.


@pytest.mark.skipif(not os.path.isfile(_HAYSTACK_TRAJ), reason="haystack fixture absent")
def test_brief_channel_source_is_v2_on_real_fixture():
    # on run 29217805592 the m1 user message is an UNCLOSED legend (no closed brief blocks),
    # so both the legacy counter and the v2 brief channel are 0 — brief_delivered=0 is HONEST
    # here (the facts ride runtime tool blocks), and the v2 ledger schema is the mini v2.
    led = cl.ledger_from_trajectory_path(_HAYSTACK_TRAJ)
    assert led["schema"] == "gt.consumption_ledger.v2"
    assert _brief_channel_count(_HAYSTACK_TRAJ) == 0
    # the runtime facts ARE counted (the delivery counter is not blind), proving the source is
    # the real v2 ledger and not a hardcoded zero.
    assert led["gt_blocks_delivered"] > 0
