#!/usr/bin/env python3
"""TTD for gt.consumption_ledger.v2 — mini-swe-agent schema + monotone receipt ladder.

Reference defect (run 29217805592): ``ledger_from_trajectory_path`` read the OLD
pier/OH step-list keys (``data.get("trajectory")``/``.get("steps")``) and returned
``gt_blocks_delivered: 0`` on a mini-swe-agent trajectory that actually carried 15
model-visible GT blocks (the runtime ledger recorded 21 delivered rows). These tests
pin the reader/writer contract and the receipt ladder.

Written RED-first: the mini-shape pin below fails against the v1 code (returns 0).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MOD_PATH = os.path.join(_REPO, "scripts", "swebench", "consumption_ledger.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("consumption_ledger_v2_test", _MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CL = _load_module()


def _assistant(content: str, *commands: str) -> dict:
    """A mini-swe-agent assistant message with model-authored prose + emitted commands."""
    actions = [{"command": c, "tool_call_id": f"call_{i}"} for i, c in enumerate(commands)]
    tool_calls = [
        {
            "index": i,
            "type": "function",
            "id": f"call_{i}",
            "function": {"name": "bash", "arguments": json.dumps({"command": c})},
        }
        for i, c in enumerate(commands)
    ]
    return {"role": "assistant", "content": content, "extra": {"actions": actions}, "tool_calls": tool_calls}


def _tool(content: str) -> dict:
    return {"role": "tool", "content": content}


def _evidence_block(file_path: str, symbol: str = "handle_request") -> str:
    return (
        f'<gt-evidence kind="post_view" file="{file_path}">\n'
        f"[WITNESS] {symbol} called by -> pkg/caller.py:12 `{symbol}(x)`\n"
        f"[SIBLINGS] Widget, {symbol}(self, x) -> None\n"
        f"</gt-evidence>"
    )


# --------------------------------------------------------------------------- #
# RED PIN — mini shape must be read; delivered=1, receipt>=3 (acted)
# --------------------------------------------------------------------------- #
def test_mini_shape_delivered_and_acted_red_pin():
    """FAILS against v1 (returns gt_blocks_delivered=0 on mini messages[])."""
    block = _evidence_block("a/b.py", symbol="widget_run")
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "info": {},
        "messages": [
            {"role": "system", "content": "You are a shell agent."},
            {"role": "user", "content": "<pr_description>fix the widget</pr_description>"},
            {"role": "assistant", "content": "Let me look at the code."},
            _tool(f"<returncode>0</returncode>\n<output>\ncode here\n{block}</output>"),
            _assistant("Now I will edit b.py to fix widget_run.", "sed -i 's/old/new/' a/b.py"),
            _tool("<returncode>0</returncode>\n<output>\ndone</output>"),
        ],
    }
    out = CL.build_consumption_ledger(traj)
    assert out["schema"] == "gt.consumption_ledger.v2"
    assert out["gt_blocks_delivered"] == 1, out
    entries = [e for e in out["entries"] if e.get("receipt")]
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "l3b.evidence"
    assert e["file_path"] == "a/b.py"
    assert e["receipt"] >= 3, e
    assert e["acted_msg_index"] == 4
    # content_sha256_16 is the exact block bytes, 16 hex chars
    assert e["content_sha256_16"] == hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]
    assert len(e["content_sha256_16"]) == 16
    # consumption == action
    assert out["gt_blocks_consumed"] == 1


def test_path_via_trajectory_path(tmp_path):
    """ledger_from_trajectory_path must read the mini file, not return empty."""
    block = _evidence_block("a/b.py", symbol="widget_run")
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "messages": [
            {"role": "user", "content": "task"},
            _tool(f"<output>{block}</output>"),
            _assistant("Editing b.py", "vim a/b.py"),
        ],
    }
    p = tmp_path / "mini-swe-agent.trajectory.json"
    p.write_text(json.dumps(traj), encoding="utf-8")
    out = CL.ledger_from_trajectory_path(str(p))
    assert out["gt_blocks_delivered"] == 1, out


# --------------------------------------------------------------------------- #
# LEVEL-2 GUARD — entity named ONLY in tool output must NOT earn level 2
# --------------------------------------------------------------------------- #
def test_level2_requires_assistant_text_not_tool_output():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "messages": [
            {"role": "user", "content": "task"},
            _tool(f"<output>{block}</output>"),
            # assistant says nothing about the entity, runs an unrelated command
            _assistant("Let me check something else.", "ls /tmp"),
            # a LATER tool message echoes the entity — must NOT count as reference
            _tool("<output>zoomlevel is defined in z/zoom.py here</output>"),
        ],
    }
    out = CL.build_consumption_ledger(traj)
    e = [x for x in out["entries"] if x.get("receipt")][0]
    assert e["receipt"] == 1, e
    assert e["referenced_msg_index"] is None
    assert e["acted_msg_index"] is None


def test_passive_read_can_reference_but_cannot_promote_acted():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    traj = {
        "messages": [
            {"role": "user", "content": "task"},
            _tool(f"<output>{block}</output>"),
            _assistant("I will inspect zoom.py before deciding.", "sed -n '1,80p' z/zoom.py"),
        ],
    }

    out = CL.build_consumption_ledger(traj)
    entry = [x for x in out["entries"] if x.get("receipt")][0]

    assert entry["receipt"] == 2
    assert entry["referenced_msg_index"] == 2
    assert entry["acted_msg_index"] is None
    assert out["gt_blocks_consumed"] == 0


def test_passive_read_without_prose_remains_delivered_only():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant("Inspecting.", "cat z/zoom.py"),
        ],
    }

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 1
    assert entry["referenced_msg_index"] is None
    assert entry["acted_msg_index"] is None


def test_named_mutation_promotes_acted_without_narration():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant("Applying the correction.", "sed -i 's/old/new/' z/zoom.py"),
        ],
    }

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 3
    assert entry["referenced_msg_index"] is None
    assert entry["acted_msg_index"] == 1


def test_named_verification_promotes_acted_without_narration():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant("Checking the result.", "python -m py_compile z/zoom.py"),
        ],
    }

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 3
    assert entry["acted_msg_index"] == 1
    assert entry["verification_followup"] is True


def test_passive_target_read_and_separate_unscoped_test_do_not_combine():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant("Checking.", "cat z/zoom.py", "pytest -q"),
        ],
    }

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 1
    assert entry["acted_msg_index"] is None
    assert entry["verification_followup"] is False


def test_structured_editor_function_name_and_target_form_one_action():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    action = {
        "role": "assistant",
        "content": "Applying the correction.",
        "tool_calls": [{
            "type": "function",
            "function": {
                "name": "str_replace",
                "arguments": json.dumps({
                    "path": "z/zoom.py",
                    "old_str": "old",
                    "new_str": "new",
                }),
            },
        }],
    }
    traj = {"messages": [_tool(f"<output>{block}</output>"), action]}

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 3
    assert entry["acted_msg_index"] == 1


def test_legacy_function_call_name_and_target_form_one_action():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    action = {
        "role": "assistant",
        "content": "Applying the correction.",
        "function_call": {
            "name": "write_file",
            "arguments": json.dumps({
                "path": "z/zoom.py",
                "content": "replacement",
            }),
        },
    }
    traj = {"messages": [_tool(f"<output>{block}</output>"), action]}

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 3
    assert entry["acted_msg_index"] == 1


def test_python_writelines_rewrite_promotes_acted():
    path = "geopandas/tools/_random.py"
    block = _evidence_block(path, symbol="sample_points")
    command = """python - <<'PY'
lines = ['replacement\n']
with open('geopandas/tools/_random.py', 'w') as f:
    f.writelines(lines)
PY"""
    traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant("Applying the rewrite.", command),
        ],
    }

    entry = CL.build_consumption_ledger(traj)["entries"][0]

    assert entry["receipt"] == 3
    assert entry["acted_msg_index"] == 1


def test_python_write_mode_open_is_mutating_but_read_mode_is_not():
    block = _evidence_block("z/zoom.py", symbol="zoomlevel")
    write_traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant("Resetting the file.", "python -c \"open('z/zoom.py', 'w').close()\""),
        ],
    }
    read_traj = {
        "messages": [
            _tool(f"<output>{block}</output>"),
            _assistant(
                "Inspecting.",
                "python -c \"open('z/zoom.py', 'r').readlines()\"",
            ),
        ],
    }

    write_entry = CL.build_consumption_ledger(write_traj)["entries"][0]
    read_entry = CL.build_consumption_ledger(read_traj)["entries"][0]

    assert write_entry["receipt"] == 3
    assert read_entry["receipt"] == 1
    assert read_entry["acted_msg_index"] is None


# --------------------------------------------------------------------------- #
# ORDERING GUARD — reference BEFORE the delivery must not count
# --------------------------------------------------------------------------- #
def test_reference_before_delivery_does_not_count():
    block = _evidence_block("w/widget.py", symbol="render_widget")
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "messages": [
            {"role": "user", "content": "task"},
            # assistant references widget.py / render_widget BEFORE the block is delivered
            _assistant(
                "I think render_widget in widget.py is relevant.",
                "sed -i 's/old/new/' w/widget.py",
            ),
            _tool(f"<output>{block}</output>"),
            # nothing after the delivery
            _assistant("Submitting now.", "echo done"),
        ],
    }
    out = CL.build_consumption_ledger(traj)
    e = [x for x in out["entries"] if x.get("receipt")][0]
    assert e["receipt"] == 1, e
    assert e["referenced_msg_index"] is None
    assert e["acted_msg_index"] is None


# --------------------------------------------------------------------------- #
# LEGACY SHAPE — old step-list still yields v1-compatible counts
# --------------------------------------------------------------------------- #
def test_legacy_step_list_v1_compatible():
    steps = [
        {"observation": "<output><gt-evidence>set_tag caller info</gt-evidence></output>", "action": ""},
        {"action": "pytest tests/test_foo.py -x", "observation": ""},
    ]
    out = CL.build_consumption_ledger(steps)
    assert out["schema"] == "gt.consumption_ledger.v1"
    assert out["gt_blocks_delivered"] == 1, out
    for k in (
        "gt_blocks_delivered",
        "gt_blocks_consumed",
        "gt_blocks_verification_followup",
        "gt_blocks_hard_enforced",
        "gt_blocks_enforced",
    ):
        assert k in out
    # legacy dict-with-trajectory shape also still works
    out2 = CL.build_consumption_ledger({"trajectory": steps})
    assert out2["schema"] == "gt.consumption_ledger.v1"
    assert out2["gt_blocks_delivered"] == 1


# --------------------------------------------------------------------------- #
# JOIN — synthetic runtime ledger joins to trajectory blocks
# --------------------------------------------------------------------------- #
def test_runtime_ledger_join(tmp_path):
    block = _evidence_block("pkg/mod.py", symbol="do_thing")
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "messages": [
            {"role": "user", "content": "task"},
            _assistant("look", "cat pkg/mod.py"),  # tool ordinal 1 comes next
            _tool(f"<output>{block}</output>"),
            _assistant("Editing mod.py do_thing.", "vim pkg/mod.py"),
        ],
    }
    tj = tmp_path / "mini-swe-agent.trajectory.json"
    tj.write_text(json.dumps(traj), encoding="utf-8")

    ledger = tmp_path / "gt_runtime_ledger_inst.jsonl"
    row = {
        "layer": "l3b.evidence",
        "event_type": "post_view",
        "file_path": "pkg/mod.py",
        "outcome": "delivered",
        "reason": "",
        "chars_delivered": len(block),  # exact match
        "iteration": 1,
        "timestamp_ms": 1,
    }
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    out = CL.ledger_from_trajectory_path(str(tj), runtime_ledger_path=str(ledger))
    assert out["join_rate"] == 1.0, out
    joined = [e for e in out["entries"] if e.get("joined")]
    assert len(joined) == 1
    assert joined[0]["file_path"] == "pkg/mod.py"
    # auto-glob (no explicit path) must also find the sibling ledger
    out2 = CL.ledger_from_trajectory_path(str(tj))
    assert out2["join_rate"] == 1.0, out2


def test_unjoined_ledger_rows_are_not_dropped(tmp_path):
    """A host-only delivered row with no visible block -> entry joined=false."""
    block = _evidence_block("pkg/mod.py", symbol="do_thing")
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "messages": [
            {"role": "user", "content": "task"},
            _tool(f"<output>{block}</output>"),
        ],
    }
    tj = tmp_path / "mini-swe-agent.trajectory.json"
    tj.write_text(json.dumps(traj), encoding="utf-8")
    rows = [
        {"layer": "l3b.evidence", "event_type": "post_view", "file_path": "pkg/mod.py",
         "outcome": "delivered", "chars_delivered": len(block), "iteration": 1},
        {"layer": "spec.obligation", "event_type": "post_view", "file_path": "pkg/other.py",
         "outcome": "delivered", "chars_delivered": 999, "iteration": 40},
    ]
    ledger = tmp_path / "gt_runtime_ledger_inst.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = CL.ledger_from_trajectory_path(str(tj), runtime_ledger_path=str(ledger))
    assert out["join_rate"] == 0.5, out
    host_only = [e for e in out["entries"] if e.get("joined") is False and e.get("source") == "ledger_only"]
    assert any(e["kind"] == "spec.obligation" for e in host_only), out["entries"]


# --------------------------------------------------------------------------- #
# REAL ARTIFACT SMOKE — skip if the run artifacts are absent
# --------------------------------------------------------------------------- #
_HAYSTACK_DIR = r"D:/gt_runs/29217805592/art/deepset-ai__haystack-8489"
_HAYSTACK_TRAJ = os.path.join(_HAYSTACK_DIR, "mini-swe-agent.trajectory.json")
_HAYSTACK_LEDGER = os.path.join(
    _HAYSTACK_DIR, "gt_runtime_ledger_deepset-ai__haystack-8489.jsonl"
)


@pytest.mark.skipif(
    not os.path.isfile(_HAYSTACK_TRAJ), reason="haystack run artifact absent"
)
def test_real_haystack_artifact_smoke():
    out = CL.ledger_from_trajectory_path(
        _HAYSTACK_TRAJ, runtime_ledger_path=_HAYSTACK_LEDGER
    )
    assert out["gt_blocks_delivered"] >= 10, out["gt_blocks_delivered"]
    assert out["join_rate"] > 0.5, out["join_rate"]
    # per_class is a table; every visible block accounted for
    total = sum(v["delivered"] for v in out["per_class"].values())
    assert total == out["gt_blocks_delivered"]
