"""GT_OBLIGATIONS_V2 — T2 workspace-artifact channel (plan §8, t11 artifact leg).

Pins: the generalized writer keeps the brief marker contract and refuses paths
outside .groundtruth/ (the patch-exclude rule keys on that dir). The T2 upfront
CHECKLIST ship is RETIRED (2026-07-08) — see test_t2_checklist_ship_retired."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artifact_deepswe"))

import gt_agent  # noqa: E402


class _Env:
    def __init__(self, stdout: str):
        self._stdout = stdout
        self.commands: list[str] = []

    async def exec(self, command: str, timeout_sec: int = 30):
        self.commands.append(command)
        return SimpleNamespace(stdout=self._stdout, returncode=0)


@pytest.mark.asyncio
async def test_writer_refuses_outside_groundtruth():
    env = _Env("GT_OBL_ARTIFACT_OK /repo")
    assert await gt_agent._write_workspace_artifact(
        "obligations.md", "content", env, marker="GT_OBL_ARTIFACT") is None
    assert not env.commands  # never even ran


@pytest.mark.asyncio
async def test_writer_marker_parameterized_and_path_returned():
    env = _Env("GT_OBL_ARTIFACT_OK /repo")
    art = await gt_agent._write_workspace_artifact(
        ".groundtruth/obligations.md", "- [ ] clause", env,
        marker="GT_OBL_ARTIFACT")
    assert art == "/repo/.groundtruth/obligations.md"
    assert "GT_OBL_ARTIFACT_OK" in env.commands[0]
    assert "check-ignore" in env.commands[0]  # fail-closed self-verify retained


@pytest.mark.asyncio
async def test_brief_wrapper_contract_unchanged():
    env = _Env("GT_BRIEF_ARTIFACT_OK /repo")
    art = await gt_agent._write_brief_artifact("brief text", env)
    assert art == "/repo/" + gt_agent._GT_ARTIFACT_REL
    assert "GT_BRIEF_ARTIFACT_OK" in env.commands[0]


def test_t2_checklist_ship_retired():
    """Move-1 (2026-07-08, witness-diagnosed regression): the T2 upfront
    obligations CHECKLIST ship is retired — it handed the agent a completion
    criterion easier than the grader (true-myth 1.0 -> 0.0 on both surfaces).
    The reader that gated the ship (_read_obligations_md) is the load-bearing
    removal: re-introducing the ship would have to re-introduce it. Its
    ground-truth replacement is the build guard in gt_mini_patch, not a checklist."""
    assert not hasattr(gt_agent, "_read_obligations_md")
    assert not hasattr(gt_agent, "_obligations_v2_artifact_dir")
