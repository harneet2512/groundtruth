from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _agent_module():
    path = ROOT / "artifact_deepswe" / "gt_agent.py"
    spec = importlib.util.spec_from_file_location("gt_agent_control_shipment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_control_participation_schema_is_shipped_to_task_container(monkeypatch) -> None:
    agent = _agent_module()
    assert "control_participation.py" in agent._PRODUCT_PACKAGE_MODULES["runtime"]
    assert agent._PRODUCT_PACKAGE_FILES["runtime"]["control_participation.py"]
    assert (
        "groundtruth.runtime.control_participation"
        in agent._INJECTED_GT_MODULES
    )
    assert "groundtruth.runtime.feature_lineage" in agent._INJECTED_GT_MODULES
    assert "groundtruth.runtime.shadow_holdout" in agent._INJECTED_GT_MODULES
    assert "groundtruth.runtime.rl_profile" in agent._INJECTED_GT_MODULES
    assert "groundtruth.runtime" in agent._INJECTED_GT_PACKAGES
    if agent.InstallStep is None:
        @dataclass
        class _InstallStep:
            user: str
            run: str

        monkeypatch.setattr(agent, "InstallStep", _InstallStep)
    runs = "\n".join(step.run for step in agent._inject_steps_b64())
    assert "> /opt/gt/groundtruth/runtime/control_participation.py" in runs
    assert "> /opt/gt/groundtruth/runtime/rl_profile.py" in runs
