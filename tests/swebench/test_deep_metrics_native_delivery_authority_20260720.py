"""D-V (run6 telegram: gt_delivery.evidence_delivered=0 vs runtime_ledger
delivered_count=11): the deep-metrics showcase counted delivery by scanning agent
observations for literal `<gt-*>` tags. Native delivery is TAGLESS (native_render
rides the model's own channel), so the tag scan structurally reads 0 for every
native form — reporting a delivered run as 0-delivered.

Fix: surface an authoritative `delivered_total_authoritative` sourced from the
sealed runtime ledger (content_sha256_16), with `delivery_authority` naming the
source. Additive (existing tag-scan fields untouched -> no consumer regression);
falls back to the tag-scan sum only when no ledger is present.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS_PATH = ROOT / "scripts" / "swebench" / "gt_deep_metrics.py"


def _load():
    spec = importlib.util.spec_from_file_location("gt_deep_metrics_dv", METRICS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_tagless_trajectory(tmp_path: Path, task: str, n_steps: int = 6) -> Path:
    agent_dir = tmp_path / "jobs" / "run" / f"{task}__x" / "agent"
    agent_dir.mkdir(parents=True)
    # brief + observations carry NO <gt-*> tags (native delivery is tagless)
    messages = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "<pr_description>fix the bug</pr_description>"},
    ]
    for step in range(1, n_steps + 1):
        cmd = "str_replace src/a.py old new" if step == 3 else f"cat src/f{step}.py"
        messages.append({
            "role": "assistant", "content": f"thought {step}",
            "tool_calls": [{"function": {"arguments": json.dumps({"command": cmd})}}],
        })
        # native (tagless) evidence lines — indistinguishable from tool output
        messages.append({"role": "tool",
                         "content": f"src/a.py:{step}: note: get_x() caller"})
    messages.append({"role": "exit", "content": "done"})
    traj = {
        "trajectory_format": "mini-swe-agent",
        "info": {"exit_status": "Submitted",
                 "submission": "diff --git a/src/a.py b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n",
                 "config": {"model": {"model_name": "deepseek/deepseek-v4-flash"}},
                 "model_stats": {"api_calls": n_steps, "instance_cost": 0.0}},
        "messages": messages,
    }
    p = agent_dir / "mini-swe-agent.trajectory.json"
    p.write_text(json.dumps(traj), encoding="utf-8")
    return agent_dir


def _write_ledger(agent_dir: Path, task: str, n_delivered: int = 11) -> None:
    rows = []
    for i in range(n_delivered):
        rows.append({
            "layer": "l3b.evidence", "event_type": "post_edit",
            "outcome": "delivered", "reason": "",
            "chars_delivered": 40, "iteration": i,
            "content_sha256_16": f"{i:016x}",  # a real seal -> counted delivery
        })
    (agent_dir / f"gt_runtime_ledger_{task}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_native_delivery_surfaced_from_sealed_ledger(tmp_path):
    mod = _load()
    task = "telegram-bug"
    agent_dir = _write_tagless_trajectory(tmp_path, task)
    _write_ledger(agent_dir, task, n_delivered=11)

    deep = mod.build(task, str(tmp_path))
    gd = deep["gt_delivery"]
    # the OLD symptom persists in the tag-scan field (tagless -> 0) ...
    assert gd["evidence_delivered"] == 0
    # ... but the sealed ledger authority is present and correct ...
    assert gd["runtime_ledger"]["delivered_count"] == 11
    # ... and the FIX: the showcase now reports the authoritative native total.
    assert gd["delivered_total_authoritative"] == 11, (
        "a native-delivered run must not be reported as 0-delivered")
    assert gd["delivery_authority"] == "runtime_ledger_seal"


def test_falls_back_to_tag_scan_when_no_ledger(tmp_path):
    # no ledger present -> authority is the tag scan (legacy OH path), byte-identical
    mod = _load()
    task = "no-ledger-task"
    _write_tagless_trajectory(tmp_path, task)  # tagless, and NO ledger written
    deep = mod.build(task, str(tmp_path))
    gd = deep["gt_delivery"]
    assert gd["delivery_authority"] == "tag_scan"
    # tagless + no ledger -> honest 0 (no false inflation from text heuristics)
    assert gd["delivered_total_authoritative"] == 0
