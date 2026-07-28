"""Wave-7 RED contract for the Mini-SWE canonical-runtime cutover.

The paid runner is the architectural choke point.  Under an active GT profile it
must attach the attempt-scoped canonical runtime and its provider boundary after
the concrete model/agent exist and before ``agent.run`` can spend a provider
call.  The old observation-batch appender is not an acceptable substitute.

These tests deliberately exercise both sides:

* GT active: native task bytes stay native, canonical attachment is mandatory,
  and legacy direct-delivery plumbing is unreachable.
* GT off: no GT module or provider wrapper is installed and the task reaches the
  agent byte-for-byte.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any

from artifact_deepswe import gt_headless_runner as runner


_RUNNER = Path(runner.__file__).resolve()
_SEAM = _RUNNER.with_name("gt_mini_patch.py")


class _Model:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.provider_calls = 0


class _Agent:
    n_calls = 1
    cost = 0.0

    def __init__(self, model: _Model, calls: list[str], captured: dict[str, Any]):
        self.model = model
        self.calls = calls
        self.captured = captured

    def run(self, task: str) -> dict[str, str]:
        self.calls.append("agent.run")
        self.captured["task"] = task
        return {"exit_status": "Submitted"}


class _CanonicalAttachment:
    """Small fake matching the required semantic attachment, not its internals."""

    attached = True
    attempt_runtime = object()
    provider_boundary = object()
    commitment_boundary = object()


def _install_fake_miniswe(
    monkeypatch,
    *,
    calls: list[str],
    captured: dict[str, Any],
) -> tuple[_Model, _Agent]:
    model = _Model(calls)
    agent = _Agent(model, calls, captured)

    pkg = types.ModuleType("minisweagent")
    pkg.__version__ = "2.4.5"
    agents = types.ModuleType("minisweagent.agents")

    def get_agent(
        received_model: object,
        _environment: object,
        _config: dict[str, Any],
        *,
        default_type: str,
    ) -> _Agent:
        assert received_model is model
        assert default_type == "default"
        calls.append("agent.built")
        return agent

    agents.get_agent = get_agent
    config = types.ModuleType("minisweagent.config")
    config.get_config_from_spec = lambda _path: {
        "model": {},
        "environment": {},
        "agent": {},
    }
    environments = types.ModuleType("minisweagent.environments")
    environments.get_environment = (
        lambda _config, *, default_type: object()
    )
    models = types.ModuleType("minisweagent.models")

    def get_model(*, config: dict[str, Any]) -> _Model:
        assert config["model_name"] == "fixture/model"
        calls.append("model.built")
        return model

    models.get_model = get_model
    for name, module in (
        ("minisweagent", pkg),
        ("minisweagent.agents", agents),
        ("minisweagent.config", config),
        ("minisweagent.environments", environments),
        ("minisweagent.models", models),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return model, agent


def _env(tmp_path: Path, *, baseline: bool = False) -> dict[str, str]:
    value = {
        "GT_RUN_MODEL": "fixture/model",
        "GT_RUN_TASK": "Fix exactly this issue.",
        "GT_RUN_CONFIG": str(tmp_path / "agent.yaml"),
        "GT_RUN_OUTPUT": str(tmp_path / "trajectory.json"),
        "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
        "GT_BRIEF_FILE": str(tmp_path / "brief.txt"),
        "GT_RL_PROFILE": "2",
    }
    if baseline:
        value["GT_BASELINE"] = "1"
    return value


def _run_function() -> ast.FunctionDef:
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )


def _call_lines(function: ast.FunctionDef, name: str) -> list[int]:
    found: list[int] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        called = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if called == name:
            found.append(node.lineno)
    return sorted(found)


def test_canonical_attachment_is_after_model_and_agent_but_before_run() -> None:
    """Construction order is a correctness boundary, not an implementation detail."""

    function = _run_function()
    model_line = _call_lines(function, "get_model")
    agent_line = _call_lines(function, "get_agent")
    attach_line = _call_lines(function, "install_canonical_runtime")
    run_line = _call_lines(function, "run")

    assert len(model_line) == len(agent_line) == len(attach_line) == len(run_line) == 1
    assert model_line[0] < agent_line[0] < attach_line[0] < run_line[0]


def test_active_profile_requires_canonical_boundary_before_agent_or_provider_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A working legacy batch wrapper cannot satisfy the new active-profile gate."""

    calls: list[str] = []
    captured: dict[str, Any] = {}
    model, _agent = _install_fake_miniswe(
        monkeypatch,
        calls=calls,
        captured=captured,
    )
    legacy_calls: list[str] = []
    patch = types.ModuleType("gt_mini_patch")
    patch._PATCHED_CLASSES = ["fixture.Environment"]

    def legacy_install(_target: object) -> bool:
        legacy_calls.append("legacy.install")
        return True

    patch.install_observation_batch_commit = legacy_install
    patch.ledger_write_failures = lambda: 0
    monkeypatch.setitem(sys.modules, "gt_mini_patch", patch)

    assert runner.run(_env(tmp_path)) == 2
    assert "agent.run" not in calls
    assert model.provider_calls == 0
    assert legacy_calls == [], "the removed batch appender must not be a fallback"


def test_active_profile_installs_one_canonical_attachment_before_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    captured: dict[str, Any] = {}
    model, agent = _install_fake_miniswe(
        monkeypatch,
        calls=calls,
        captured=captured,
    )
    received: dict[str, Any] = {}
    patch = types.ModuleType("gt_mini_patch")
    patch._PATCHED_CLASSES = ["fixture.Environment"]

    def install_canonical_runtime(*args: object, **kwargs: object) -> _CanonicalAttachment:
        calls.append("canonical.attached")
        received["args"] = args
        received["kwargs"] = kwargs
        return _CanonicalAttachment()

    def removed_legacy_install(_target: object) -> bool:
        raise AssertionError("legacy observation appender must be unreachable")

    patch.install_canonical_runtime = install_canonical_runtime
    patch.install_observation_batch_commit = removed_legacy_install
    patch.ledger_write_failures = lambda: 0
    monkeypatch.setitem(sys.modules, "gt_mini_patch", patch)

    assert runner.run(_env(tmp_path)) == 0
    assert calls.index("model.built") < calls.index("agent.built")
    assert calls.index("agent.built") < calls.index("canonical.attached")
    assert calls.index("canonical.attached") < calls.index("agent.run")
    attached_values = tuple(received["args"]) + tuple(received["kwargs"].values())
    assert model in attached_values
    assert agent in attached_values


def test_active_task_and_brief_are_not_directly_mutated_or_premarked_delivered(
    tmp_path: Path,
) -> None:
    """The task-start brief is evidence input; it is not a direct transcript append."""

    brief = tmp_path / "brief.txt"
    brief.write_text("Repository-derived brief bytes.", encoding="utf-8")
    ledger = tmp_path / "runtime.jsonl"
    env = _env(tmp_path)
    env["GT_INSEAM_METRICS"] = "1"

    assert runner._resolve_task(env).encode("utf-8") == env[
        "GT_RUN_TASK"
    ].encode("utf-8")
    assert not ledger.exists(), "joining has not happened, so DELIVERED cannot be recorded"


def test_legacy_step0_delivery_owners_are_absent_from_live_runner() -> None:
    """Dead host-side owners must not masquerade as canonical delivery plumbing."""
    legacy_names = (
        "_brief_delivery_extra",
        "_persist_brief_localization_attestations",
        "_persist_brief_obligations_attestations",
        "_record_brief_delivery",
        "_BRIEF_LEDGER_WRITE_FAILURES",
    )
    assert not [name for name in legacy_names if hasattr(runner, name)]


def test_legacy_direct_delivery_paths_are_absent_from_live_runner_and_seam() -> None:
    runner_source = _RUNNER.read_text(encoding="utf-8")
    assert "install_observation_batch_commit" not in runner_source

    tree = ast.parse(_SEAM.read_text(encoding="utf-8"))
    augment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_augment_output"
        ),
        None,
    )
    if augment is None:
        return
    called = {
        (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        for node in ast.walk(augment)
        if isinstance(node, ast.Call)
    }
    forbidden = {
        "_lane_a_deliver",
        "_commit_prepared_lane",
        "_gt_gateway_append",
        "_gt_deliver_append",
        "_gt_gateway_deliver",
        "_deliver_gate_winner",
        "_commit_prepared_steer",
        "_commit_precommitted_batch_dose",
        "_commit_batch_arbitration",
        "_ss_record_delivered",
    }
    assert called.isdisjoint(forbidden), sorted(called & forbidden)


def test_gt_off_is_byte_identical_and_installs_no_gt_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    captured: dict[str, Any] = {}
    _model, _agent = _install_fake_miniswe(
        monkeypatch,
        calls=calls,
        captured=captured,
    )
    poisoned = types.ModuleType("gt_mini_patch")

    def must_not_install(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("GT-off must not install any canonical or legacy boundary")

    poisoned.install_canonical_runtime = must_not_install
    poisoned.install_observation_batch_commit = must_not_install
    monkeypatch.setitem(sys.modules, "gt_mini_patch", poisoned)
    (tmp_path / "brief.txt").write_text(
        "This must not reach the baseline model.",
        encoding="utf-8",
    )
    env = _env(tmp_path, baseline=True)

    assert runner.run(env) == 0
    assert captured["task"].encode("utf-8") == env["GT_RUN_TASK"].encode("utf-8")
    assert calls == ["model.built", "agent.built", "agent.run"]
