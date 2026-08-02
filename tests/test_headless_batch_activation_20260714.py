"""Pins the paid headless runner's observation-batch activation boundary."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from artifact_deepswe import gt_headless_runner as runner


class _Agent:
    n_calls = 1
    cost = 0.0

    def __init__(self, captured: dict):
        self.model = object()
        self._captured = captured

    def run(self, task):
        self._captured["task"] = task
        ledger = Path(self._captured["ledger"])
        ledger.write_text(json.dumps({
            "layer": "fixture", "event_type": "fixture", "file_path": "",
            "outcome": "eligible", "reason": "fixture", "chars_delivered": 0,
            "iteration": 0,
        }) + "\n", encoding="utf-8")
        return {"exit_status": "Submitted"}


def _install_fake_runtime(
    monkeypatch, captured: dict, *, install_result: bool, write_failures: int = 0,
) -> None:
    agent = _Agent(captured)
    pkg = types.ModuleType("minisweagent")
    pkg.__version__ = "2.4.5"
    agents = types.ModuleType("minisweagent.agents")
    agents.get_agent = lambda *a, **k: agent
    config = types.ModuleType("minisweagent.config")
    config.get_config_from_spec = lambda p: {"model": {}, "environment": {}, "agent": {}}
    environments = types.ModuleType("minisweagent.environments")
    environments.get_environment = lambda *a, **k: object()
    models = types.ModuleType("minisweagent.models")
    models.get_model = lambda *a, **k: agent.model
    for name, module in (
        ("minisweagent", pkg),
        ("minisweagent.agents", agents),
        ("minisweagent.config", config),
        ("minisweagent.environments", environments),
        ("minisweagent.models", models),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    patch = types.ModuleType("gt_mini_patch")
    patch._PATCHED_CLASSES = ["minisweagent.environments.local.LocalEnvironment"]

    def install_canonical_runtime(*, model, agent, env, task):
        if not install_result:
            return None
        return types.SimpleNamespace(
            attached=True,
            attempt_runtime=object(),
            provider_boundary=object(),
            commitment_boundary=object(),
        )

    patch.install_canonical_runtime = install_canonical_runtime
    patch.ledger_write_failures = lambda: write_failures
    monkeypatch.setitem(sys.modules, "gt_mini_patch", patch)


def _env(tmp_path: Path) -> dict[str, str]:
    env = {
        "GT_RUN_MODEL": "deepseek/deepseek-v4-flash",
        "GT_RUN_TASK": "Fix the issue.",
        "GT_BRIEF_FILE": str(tmp_path / "missing-brief.txt"),
        "GT_RUN_CONFIG": str(tmp_path / "config.yaml"),
        "GT_RUN_OUTPUT": str(tmp_path / "trajectory.json"),
        "GT_RUNTIME_LEDGER": str(tmp_path / "gt_runtime_ledger_task.jsonl"),
        "GT_RL_PROFILE": "2",
        "GT_GLOBAL_ARBITER": "1",
    }
    return env


def test_profile2_install_failure_stops_before_agent_run(tmp_path, monkeypatch):
    captured: dict = {}
    _install_fake_runtime(monkeypatch, captured, install_result=False)

    assert runner.run(_env(tmp_path)) == 2
    assert "task" not in captured
    receipt = json.loads((tmp_path / "gt_batch_activation.json").read_text(encoding="utf-8"))
    assert receipt["required"] is True
    assert receipt["wrapper_attached"] is False
    assert receipt["result"] == "install_unavailable"
    assert receipt["mini_swe_version"] == "2.4.5"


def test_profile2_install_success_is_receipted_before_agent_run(tmp_path, monkeypatch):
    captured: dict = {}
    captured["ledger"] = str(tmp_path / "gt_runtime_ledger_task.jsonl")
    _install_fake_runtime(monkeypatch, captured, install_result=True)

    assert runner.run(_env(tmp_path)) == 0
    assert captured["task"] == "Fix the issue."
    receipt = json.loads((tmp_path / "gt_batch_activation.json").read_text(encoding="utf-8"))
    assert receipt["schema"] == "gt.batch_activation.v1"
    assert receipt["required"] is True
    assert receipt["wrapper_attached"] is True
    assert receipt["result"] == "installed"
    assert receipt["mini_swe_version"] == "2.4.5"
    # A proved canonical attachment owns RuntimeJournal persistence. The
    # historical JSONL written by this fixture is deliberately not attested.
    assert not (tmp_path / "gt_runtime_ledger_attestation_task.json").exists()


def test_profile2_ledger_write_failure_is_terminal_and_attested(tmp_path, monkeypatch):
    captured = {"ledger": str(tmp_path / "gt_runtime_ledger_task.jsonl")}
    _install_fake_runtime(
        monkeypatch, captured, install_result=True, write_failures=1,
    )

    assert runner.run(_env(tmp_path)) == 2
    assert not (tmp_path / "gt_runtime_ledger_attestation_task.json").exists()


def test_workflow_pins_and_requires_batch_receipt():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "swebench_live_lite_full.yml"
    ).read_text(encoding="utf-8")
    assert workflow.count("mini-swe-agent==2.4.5") == 4
    assert "GT_BATCH_UNPROVEN" in workflow
    assert "trial_results/gt_artifacts/gt_batch_activation.json" in workflow
    assert "GT_LEDGER_ATTESTATION_UNPROVEN" in workflow
    assert "gt_runtime_ledger_attestation_${GT_TASK_ID}.json" in workflow
