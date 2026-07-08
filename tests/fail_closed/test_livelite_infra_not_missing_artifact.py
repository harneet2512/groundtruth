"""Regression: a Live-Lite task that RAN + was SCORED must not mislabel INFRA_MISSING_ARTIFACT.

Live-Lite (swebench_live_lite_full.yml) writes no result.json / agent-trajectory under jobs/; it bridges
the OFFICIAL-eval reward to jobs/*/*__*/verifier/reward.json (trajectory + eval report live under
trial_results/). detect_infra_subtype scanned only the DeepSWE/pier layout, so it falsely classified
every ran-and-scored Live-Lite task as INFRA_MISSING_ARTIFACT (witness run 28914877553, aiogram, which
resolved=False via the official eval — a legit AGENT/unresolved outcome, NOT infra). The bridged reward
== "the harness ran and scored the task" -> not infra. Real infra (nothing produced) MUST still fire.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "verify"))
from deepswe_outcome import detect_infra_subtype  # noqa: E402


def _bridge_reward(root, ts="1700", task="aiogram__aiogram-1594__ll", reward=0.0):
    d = os.path.join(root, ts, task, "verifier")
    os.makedirs(d)
    json.dump({"reward": reward}, open(os.path.join(d, "reward.json"), "w", encoding="utf-8"))


def test_livelite_bridged_reward_is_not_missing_artifact(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _bridge_reward(str(jobs))  # ran + scored (unresolved), no result.json/agent-traj (Live-Lite shape)
    assert detect_infra_subtype(str(jobs)) is None


def test_livelite_bridged_reward_zero_still_not_infra(tmp_path):
    # reward=0.0 (unresolved / empty patch) is an AGENT outcome, NOT infra — the harness still ran.
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _bridge_reward(str(jobs), reward=0.0)
    assert detect_infra_subtype(str(jobs)) != "INFRA_MISSING_ARTIFACT"


def test_genuinely_empty_jobs_still_missing_artifact(tmp_path):
    # Real infra: nothing produced at all (no result / traj / reward) -> MUST still fire.
    jobs = tmp_path / "jobs"
    (jobs / "1700" / "x__ll").mkdir(parents=True)
    assert detect_infra_subtype(str(jobs)) == "INFRA_MISSING_ARTIFACT"


def test_deepswe_result_json_unaffected(tmp_path):
    jobs = tmp_path / "jobs"
    d = jobs / "1700" / "task__abc" / "agent"
    d.mkdir(parents=True)
    json.dump({"agent_result": {}}, open(jobs / "1700" / "task__abc" / "result.json", "w"))
    json.dump([], open(d / "mini-swe-agent.trajectory.json", "w"))
    assert detect_infra_subtype(str(jobs)) is None
