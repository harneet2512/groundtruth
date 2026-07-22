"""Regression tests for generalized mandatory-metric semantics.

These fixtures exercise provider-neutral usage accounting, receipt-backed recovery
compliance, and authoritative repository edit paths without task/repository literals.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWE = ROOT / "scripts" / "swebench"
if str(SWE) not in sys.path:
    sys.path.insert(0, str(SWE))


def _load(name: str):
    path = SWE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = _load("gt_performance_metrics")
bi = _load("gt_behavioral_impact")
dm = _load("gt_deep_metrics")


def _assistant(command: str, *, timestamp: float, usage: dict | None = None) -> dict:
    extra: dict = {"timestamp": timestamp, "actions": [{"command": command}]}
    if usage is not None:
        extra["response"] = {"usage": usage}
    return {
        "role": "assistant",
        "content": "working",
        "tool_calls": [{"function": {
            "name": "bash", "arguments": json.dumps({"command": command}),
        }}],
        "extra": extra,
    }


def _write_trajectory(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "mini-swe-agent.trajectory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_chat_usage_is_summed_when_model_stats_has_no_token_rollup(tmp_path: Path) -> None:
    trajectory = {
        "messages": [
            _assistant("cat src/a.py", timestamp=1.0, usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            }),
            {"role": "tool", "content": "a"},
            _assistant("pytest -q", timestamp=2.0, usage={
                "prompt_tokens": 150,
                "completion_tokens": 15,
                "prompt_cache_hit_tokens": 120,
                "prompt_cache_miss_tokens": 30,
            }),
        ],
        "info": {"model_stats": {"api_calls": 2, "instance_cost": 0.25}},
    }
    path = _write_trajectory(tmp_path, trajectory)

    result = pm.compute_performance_metrics(str(path), str(tmp_path), gold_files=[])
    tokens = result["token_efficiency"]

    assert tokens["total_tokens_in"] == 250
    assert tokens["total_tokens_out"] == 25
    assert tokens["cache_hit_rate"] == 0.8
    assert tokens["usage_source"] == "message_usage"


def test_responses_usage_shape_and_missing_usage_are_truthful(tmp_path: Path) -> None:
    trajectory = {
        "messages": [{
            "role": None,
            "output": [{"type": "function_call", "arguments": json.dumps({
                "command": "cat src/a.py",
            })}],
            "usage": {
                "input_tokens": 90,
                "output_tokens": 9,
                "input_tokens_details": {"cached_tokens": 60},
            },
        }],
        "info": {"model_stats": {"api_calls": 1}},
    }
    path = _write_trajectory(tmp_path, trajectory)
    measured = pm.compute_performance_metrics(str(path), str(tmp_path), gold_files=[])
    tokens = measured["token_efficiency"]
    assert tokens["total_tokens_in"] == 90
    assert tokens["total_tokens_out"] == 9
    assert tokens["cache_hit_rate"] == round(60 / 90, 8)

    empty_path = _write_trajectory(tmp_path, {
        "messages": [], "info": {"model_stats": {"api_calls": 0}},
    })
    missing = pm.compute_performance_metrics(str(empty_path), str(tmp_path), gold_files=[])
    missing_tokens = missing["token_efficiency"]
    assert missing_tokens["total_tokens_in"] is None
    assert missing_tokens["total_tokens_out"] is None
    assert missing_tokens["cache_hit_rate"] is None
    assert missing_tokens["usage_source"] == "unavailable"


def test_complete_rollup_wins_without_double_count_and_deep_matches(tmp_path: Path) -> None:
    task_dir = tmp_path / "acme__task-1"
    task_dir.mkdir()
    trajectory = {
        "messages": [_assistant("cat src/a.py", timestamp=1.0, usage={
            "prompt_tokens": 900, "completion_tokens": 90,
            "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 100,
        })],
        "info": {"model_stats": {
            "api_calls": 1,
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 75,
            "prompt_cache_miss_tokens": 25,
        }},
    }
    path = _write_trajectory(task_dir, trajectory)

    perf = pm.compute_performance_metrics(str(path), str(task_dir), gold_files=[])
    deep = dm._from_miniswe_trajectory("acme__task-1", str(task_dir))

    assert perf["token_efficiency"]["usage_source"] == "model_stats"
    assert perf["token_efficiency"]["total_tokens_in"] == 100
    assert deep["prompt_tokens"] == 100
    assert deep["completion_tokens"] == 10
    assert deep["usage_source"] == "model_stats"


def test_recovery_compliance_uses_joined_receipt_three_over_delivered() -> None:
    trajectory = {"messages": [
        {"role": "tool", "content": "first"},
        {"role": "assistant", "content": "continue"},
        {"role": "tool", "content": "second"},
        {"role": "assistant", "content": "act"},
        {"role": "tool", "content": "unjoined"},
    ]}
    ledger = {
        "runtime_ledger_path": "ledger.jsonl",
        "entries": [
            {"source": "trajectory", "joined": True, "receipt": 1,
             "msg_index": 0, "kind": "detect.loop", "chars": 5},
            {"source": "trajectory", "joined": True, "receipt": 3,
             "msg_index": 2, "kind": "recovery", "chars": 6},
            {"source": "trajectory", "joined": False, "receipt": 3,
             "msg_index": 4, "kind": "detect.loop", "chars": 8},
            {"source": "trajectory", "joined": True, "receipt": 0,
             "msg_index": 4, "kind": "detect.loop", "chars": 8},
        ],
    }

    summary = bi.analyze_trajectory(trajectory, consumption_ledger=ledger)["summary"]

    assert summary["per_tag_impact"]["recovery"]["total"] == 2
    assert summary["nudge_compliance_rate"] == 0.5
    assert summary["nudge_compliance_numerator"] == 1
    assert summary["nudge_compliance_denominator"] == 2


def test_recovery_compliance_is_null_without_delivered_recovery() -> None:
    summary = bi.analyze_trajectory(
        {"messages": [{"role": "tool", "content": "contract"}]},
        consumption_ledger={"entries": [{
            "source": "trajectory", "receipt": 3, "msg_index": 0,
            "kind": "caller_contract", "chars": 8,
        }]},
    )["summary"]
    assert summary["nudge_compliance_rate"] is None
    assert summary["nudge_compliance_denominator"] == 0


def test_runtime_post_edit_paths_enrich_without_erasing_shell_edits(tmp_path: Path) -> None:
    trajectory = {
        "messages": [
            _assistant("cat > /tmp/helper.py <<'PY'\nPY", timestamp=10.0),
            {"role": "tool", "content": "done"},
            _assistant("python /tmp/helper.py", timestamp=20.0),
            {"role": "tool", "content": "updated"},
        ],
        "info": {
            "model_stats": {"api_calls": 2},
            "submission": "diff --git a/src/gold.py b/src/gold.py\n+x\n-y",
        },
    }
    path = _write_trajectory(tmp_path, trajectory)
    ledger_path = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger_path.write_text(json.dumps({
        "event_type": "post_edit",
        "file_path": "src/gold.py",
        "iteration": 7,
        "timestamp_ms": 20_500,
        "outcome": "suppressed_hidden_only",
    }) + "\n", encoding="utf-8")
    consumption = {"runtime_ledger_path": str(ledger_path), "entries": []}

    result = pm.compute_performance_metrics(
        str(path), str(tmp_path), gold_files=["src/gold.py"],
        consumption_ledger=consumption,
    )

    loc = result["localization"]
    # The runtime post-edit enrichment still does NOT erase the shell edit from the
    # timeline (that is this test's contract) — proven by edit_path_source and
    # authoritative_post_edit_count below, which are unchanged.
    assert result["edit_path_source"] == "command_plus_runtime_post_edit"
    assert result["authoritative_post_edit_count"] == 1
    # Localization RATES now derive from edit-truth authority (the SUBMITTED PATCH),
    # not command inference: the submitted diff touches ONLY src/gold.py, so the
    # /tmp/helper.py scratch write (never in the repo diff) is not a repo edit.
    # -> n_edited=1, precision=1.0, and no non-gold edit precedes the gold edit.
    assert loc["_edit_authority"] == "submission_patch"
    assert loc["_unique_edited"] == 1
    assert loc["steps_to_gold_edit"] == 1        # gold edited at step 2 (2-1)
    assert loc["files_to_gold_edit"] == 0        # was 1 under command inference
    assert loc["localization_precision"] == 1.0  # was 0.5 under command inference


def test_direct_edit_command_remains_fallback_without_post_edit_rows(tmp_path: Path) -> None:
    trajectory = {
        "messages": [
            _assistant("sed -i 's/x/y/' src/gold.py", timestamp=1.0),
            {"role": "tool", "content": "done"},
        ],
        "info": {
            "model_stats": {"api_calls": 1},
            "submission": "diff --git a/src/gold.py b/src/gold.py\n+x\n-y",
        },
    }
    path = _write_trajectory(tmp_path, trajectory)

    result = pm.compute_performance_metrics(
        str(path), str(tmp_path), gold_files=["src/gold.py"],
        consumption_ledger={"runtime_ledger_path": "missing.jsonl", "entries": []},
    )

    assert result["localization"]["steps_to_gold_edit"] == 0
    assert result["edit_path_source"] == "command_fallback"
