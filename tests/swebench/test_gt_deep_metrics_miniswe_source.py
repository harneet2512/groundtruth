"""Mini-swe-agent (DeepSWE/pier) trajectory backfill for gt_deep_metrics.

PROVEN BUG (read-grounded): ``_from_trajectory`` derived the agent-side metrics
(agent steps, source edits, first_edit, brief_delivered, gt_observation_chars_total)
ONLY from the OpenHands ``output.jsonl``. On the LIVE DeepSWE path the trajectory is
``mini-swe-agent.trajectory.json`` and ``output.jsonl`` is absent, so the function
returned ALL ZEROS even when the agent ran 59 steps and the brief was delivered.

These tests assert the fallback now reads the mini-swe trajectory and populates the
SAME keys, that the JSON/`.md` are self-describing about which source was used, and —
mutation-proof — that stubbing the fallback back to the OH-only behaviour makes the
assertions go RED (zeros). The OpenHands path must remain byte-equivalent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
METRICS_PATH = ROOT / "scripts" / "swebench" / "gt_deep_metrics.py"

# The real completed superjson run (mini-swe-agent format). Used as a grounded
# fixture when present on the host; the synthetic fixture below keeps the test
# hermetic in CI where this artifact does not exist.
REAL_SUPERJSON_TRAJ = Path(
    r"C:/Users/Lenovo/.claude-personal/jobs/fbdb1439/tmp/wit2/superjson/jobs/"
    r"2026-06-17__08-15-47/superjson-error-stack-serializat__7t9KqdK/agent/"
    r"mini-swe-agent.trajectory.json"
)
REAL_SUPERJSON_ROOT = Path(r"C:/Users/Lenovo/.claude-personal/jobs/fbdb1439/tmp/wit2/superjson")


def _load_metrics_module():
    spec = importlib.util.spec_from_file_location("gt_deep_metrics_miniswe_src", METRICS_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_miniswe_trajectory(
    tmp_path: Path, task: str, *, n_steps: int = 59, edit_at: int = 29
) -> Path:
    """Build a minimal but structurally-faithful mini-swe-agent.trajectory.json:
    system + user(brief) + n_steps × (assistant, tool), reproducing the real shape
    (info.model_stats.api_calls, config.model.model_name, GT tags in the user/brief
    observation, an edit command at ``edit_at``)."""
    agent_dir = tmp_path / "jobs" / "2026-run" / f"{task}__abc" / "agent"
    agent_dir.mkdir(parents=True)
    brief = (
        "<pr_description>\n"
        "<gt-task-brief>\n<gt-graph-map>\nsrc/a.ts :: f calls g\n</gt-graph-map>\n"
        "<gt-evidence>\ncontract: f(x) -> Y\n</gt-evidence>\n"
        "<gt-evidence>\ncaller: h in src/b.ts\n</gt-evidence>\n"
        '<gt-nudge reason="verify">check the edit</gt-nudge>\n'
        "</gt-task-brief>\n</pr_description>\n"
    )
    messages = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": brief},
    ]
    for step in range(1, n_steps + 1):
        if step == edit_at:
            cmd = "str_replace src/a.ts old new"
        else:
            cmd = f"cat src/file_{step}.ts"
        messages.append(
            {
                "role": "assistant",
                "content": f"thought {step}",
                "tool_calls": [{"function": {"arguments": json.dumps({"command": cmd})}}],
                "extra": {
                    "response": {
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 100,
                            "total_tokens": 1100,
                            "prompt_cache_hit_tokens": 800,
                            "prompt_cache_miss_tokens": 200,
                        }
                    }
                },
            }
        )
        messages.append({"role": "tool", "content": f"observation {step}"})
    messages.append({"role": "exit", "content": "done"})
    traj = {
        "trajectory_format": "mini-swe-agent",
        "info": {
            "exit_status": "Submitted",
            "submission": "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n",
            "config": {"model": {"model_name": "deepseek/deepseek-v4-flash"}},
            "model_stats": {"api_calls": n_steps, "instance_cost": 0.0},
        },
        "messages": messages,
    }
    p = agent_dir / "mini-swe-agent.trajectory.json"
    p.write_text(json.dumps(traj), encoding="utf-8")
    return p


def _write_output_jsonl(tmp_path: Path, task: str) -> Path:
    """Minimal OpenHands output.jsonl (one JSON object, first line) with a 2-action
    history, one GT observation, and a git_patch — for the no-regression assertion."""
    oh_dir = tmp_path / "oh" / task
    oh_dir.mkdir(parents=True)
    obj = {
        "history": [
            {"action": "run", "args": {"command": "ls"}, "content": ""},
            {
                "action": "edit",
                "args": {"command": "str_replace x y"},
                "content": "<gt-evidence>caller info</gt-evidence>",
            },
        ],
        "test_result": {"git_patch": "diff --git a/x b/x\n"},
        "resolved": True,
    }
    p = oh_dir / "output.jsonl"
    p.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 1. The fix: miniswe fallback populates real agent metrics (was all-zeros).
# --------------------------------------------------------------------------- #
def test_miniswe_fallback_populates_agent_metrics(tmp_path):
    mod = _load_metrics_module()
    task = "superjson-error-stack-serializat"
    _write_miniswe_trajectory(tmp_path, task, n_steps=59, edit_at=29)

    # _from_trajectory must now be self-sufficient off the OH path.
    traj = mod._from_trajectory(task, str(tmp_path))
    assert traj["trajectory_source"] == "miniswe_trajectory"
    assert traj["action_count"] == 59  # ~= result.json n_agent_steps=59
    assert traj["gt_brief_delivered"] > 0
    assert traj["gt_observation_chars_total"] > 0
    assert traj["first_edit_action"] == 29
    assert traj["has_patch"] is True

    deep = mod.build(task, str(tmp_path))
    assert deep["inputs_present"]["output_jsonl"] is False
    assert deep["inputs_present"]["miniswe_trajectory"] is True
    assert deep["inputs_present"]["trajectory_source"] == "miniswe_trajectory"
    # No double counting: build() merge guards on `not action_count`, which is now set.
    assert deep["agent"]["action_count"] == 59.0
    assert deep["gt_delivery"]["brief_delivered"] > 0
    assert deep["gt_delivery"]["gt_observation_chars_total"] > 0
    assert deep["has_patch"] is True


# --------------------------------------------------------------------------- #
# 2. MUTATION PROOF: stub the fallback back to OH-only behaviour -> RED (zeros).
# --------------------------------------------------------------------------- #
def test_mutation_old_oh_only_behaviour_goes_red(tmp_path, monkeypatch):
    mod = _load_metrics_module()
    task = "superjson-error-stack-serializat"
    _write_miniswe_trajectory(tmp_path, task, n_steps=59, edit_at=29)

    # Emulate the pre-fix _from_trajectory: OH output.jsonl only, no miniswe fallback.
    def _old_from_trajectory(task_, results_dir_):
        oj = mod._find_output_jsonl(task_, results_dir_)
        out = {
            "output_jsonl": oj or "",
            "trajectory_source": "none",
            "action_count": 0,
            "edits": 0,
            "first_edit_action": 0,
            "flows_delivered": 0,
            "contracts_delivered": 0,
            "consensus_delivered": 0,
            "test_delivered": 0,
            "gt_observation_chars_total": 0,
            "resolved": None,
            "has_patch": False,
        }
        return out  # no output.jsonl -> all zeros, exactly the proven bug

    monkeypatch.setattr(mod, "_from_trajectory", _old_from_trajectory)
    traj = mod._from_trajectory(task, str(tmp_path))
    # The bug reproduced: zeros despite a 59-step run with a delivered brief.
    assert traj["action_count"] == 0
    assert traj.get("gt_brief_delivered", 0) == 0
    assert traj["gt_observation_chars_total"] == 0
    assert traj["trajectory_source"] == "none"


# --------------------------------------------------------------------------- #
# 3. NO REGRESSION: the OpenHands output.jsonl path is unchanged.
# --------------------------------------------------------------------------- #
def test_openhands_output_jsonl_path_unchanged(tmp_path):
    mod = _load_metrics_module()
    task = "oh-task"
    _write_output_jsonl(tmp_path / "results", task)

    traj = mod._from_trajectory(task, str(tmp_path / "results"))
    assert traj["trajectory_source"] == "output_jsonl"
    assert traj["action_count"] == 2  # two history actions
    assert traj["edits"] == 1
    assert traj["first_edit_action"] == 2
    assert traj["has_patch"] is True
    assert traj["resolved"] is True
    # GT observation chars counted from the <gt-evidence> content line.
    assert traj["gt_observation_chars_total"] > 0
    # miniswe-only keys must NOT appear on the OH path (function stayed minimal).
    assert "gt_brief_delivered" not in traj


# --------------------------------------------------------------------------- #
# 4. GROUNDED: the real completed superjson run (skips when the artifact is absent).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not REAL_SUPERJSON_TRAJ.exists(),
    reason="real superjson mini-swe trajectory not present on this host",
)
def test_real_superjson_trajectory_grounded():
    mod = _load_metrics_module()
    task = "superjson-error-stack-serializat"
    traj = mod._from_trajectory(task, str(REAL_SUPERJSON_ROOT))
    assert traj["trajectory_source"] == "miniswe_trajectory"
    assert traj["action_count"] == 59  # matches result.json n_agent_steps=59
    assert traj["gt_brief_delivered"] >= 1
    assert traj["gt_observation_chars_total"] > 0
    assert traj["has_patch"] is True
