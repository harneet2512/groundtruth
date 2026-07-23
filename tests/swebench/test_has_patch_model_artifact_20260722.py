"""has_patch / outcome must read sealed ``artifacts/model.patch`` bytes.

MEASUREMENT DEFECT (CLAUDE.md 2026-07-22 / run 29904040782):
``gt_deep_metrics.outcome=unresolved_no_patch_agent_ran`` / ``has_patch=false`` was
UNRELIABLE when DeepSWE left ``info.submission`` empty but wrote a real
``artifacts/model.patch`` (hundreds–thousands of lines). Taxonomy then lied as
"agent gave up" for patch-produced-but-failed-tests tasks.

These tests pin:
1. Empty submission + sibling ``artifacts/model.patch`` → has_patch=True,
   outcome=unresolved_with_patch (never unresolved_no_patch_agent_ran).
2. Finder resolves pier ``agent/`` → sibling ``artifacts/model.patch``.
3. PERF ``submission_found`` / ``patch_size`` also read model.patch bytes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gt_deep_metrics as dm  # noqa: E402
import gt_performance_metrics as pm  # noqa: E402


TASK = "httpx-streaming-charset"
PATCH_BODY = (
    "diff --git a/httpx/_models.py b/httpx/_models.py\n"
    "@@ -10,6 +10,9 @@\n"
    "+class DecodingError(Exception):\n"
    "+    pass\n"
    " def decode(text):\n"
    "-    return text\n"
    "+    raise DecodingError(text)\n"
)


def _write_pier_empty_submission(tmp_path: Path, *, with_patch: bool = True) -> Path:
    """Pier layout: jobs/<run>/<task>__id/{agent,artifacts}/ with empty submission."""
    trial = tmp_path / "jobs" / "2026-07-22__00" / f"{TASK}__abc"
    agent = trial / "agent"
    arts = trial / "artifacts"
    agent.mkdir(parents=True)
    arts.mkdir(parents=True)
    traj = {
        "trajectory_format": "mini-swe-agent",
        "info": {
            "exit_status": "Submitted",
            "submission": "",  # the defect shape — empty while model.patch exists
            "config": {"model": {"model_name": "deepseek/deepseek-v4-flash"}},
            "model_stats": {"api_calls": 12, "instance_cost": 0.01},
        },
        "messages": [
            {
                "role": "assistant",
                "content": "edit",
                "tool_calls": [
                    {"function": {"arguments": json.dumps({"command": "sed -i s/a/b/ httpx/_models.py"})}}
                ],
            },
            {"role": "tool", "content": "ok"},
        ],
    }
    (agent / "mini-swe-agent.trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    (trial / "verifier").mkdir(exist_ok=True)
    (trial / "verifier" / "reward.txt").write_text("0", encoding="utf-8")
    if with_patch:
        (arts / "model.patch").write_text(PATCH_BODY, encoding="utf-8")
    return tmp_path


def test_empty_submission_model_patch_is_has_patch_not_no_patch(tmp_path: Path) -> None:
    root = _write_pier_empty_submission(tmp_path, with_patch=True)
    deep = dm.build(TASK, str(root))
    assert deep["has_patch"] is True
    assert deep["outcome"] == "unresolved_with_patch"
    assert deep["outcome"] != "unresolved_no_patch_agent_ran"
    assert "model.patch" in (deep.get("patch_source") or "")
    assert (deep.get("inputs_present") or {}).get("patch_artifact")


def test_finder_from_agent_dir_resolves_sibling_artifacts(tmp_path: Path) -> None:
    root = _write_pier_empty_submission(tmp_path, with_patch=True)
    agent_dir = root / "jobs" / "2026-07-22__00" / f"{TASK}__abc" / "agent"
    found = dm._find_patch_artifact(TASK, str(agent_dir))
    assert found is not None
    assert found.endswith("model.patch")
    assert Path(found).stat().st_size > 0


def test_no_patch_artifact_stays_honest_no_patch(tmp_path: Path) -> None:
    root = _write_pier_empty_submission(tmp_path, with_patch=False)
    deep = dm.build(TASK, str(root))
    assert deep["has_patch"] is False
    assert deep["outcome"] == "unresolved_no_patch_agent_ran"


def test_perf_reads_model_patch_for_submission_stats(tmp_path: Path) -> None:
    root = _write_pier_empty_submission(tmp_path, with_patch=True)
    tj = (
        root / "jobs" / "2026-07-22__00" / f"{TASK}__abc"
        / "agent" / "mini-swe-agent.trajectory.json"
    )
    perf = pm.compute_performance_metrics(str(tj), str(root))
    assert perf["submission_found"] is True
    assert perf.get("submission_source", "").startswith("artifact:")
    assert perf["edit_quality"]["patch_size"] > 0
    assert perf["edit_quality"]["patch_files"] >= 1


def test_mutation_ignore_model_patch_goes_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the classifier ignores model.patch, the defect returns (RED)."""
    root = _write_pier_empty_submission(tmp_path, with_patch=True)
    monkeypatch.setattr(dm, "_find_patch_artifact", lambda *_a, **_k: None)
    monkeypatch.setattr(dm, "_pier_sibling_patch_paths", lambda *_a, **_k: [])
    mini = dm._from_miniswe_trajectory(TASK, str(root))
    assert mini["has_patch"] is False  # pre-fix lie reproduced
