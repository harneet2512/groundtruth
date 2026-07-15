"""CP006 — task_truth reconciler tests."""
import importlib.util
import json
import os
import tempfile

import pytest

import sys as _sys

_TT_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "swebench", "task_truth.py")
_spec = importlib.util.spec_from_file_location("task_truth", _TT_PATH)
tt = importlib.util.module_from_spec(_spec)
_sys.modules["task_truth"] = tt
_spec.loader.exec_module(tt)


def test_reconcile_witness_overrides_handoff_fail():
    signal = {
        "gt_prebuilt_active": True,
        "hook_hash_match": True,
        "gt_meta_present": True,
        "cert_verdicts": {
            "graph_certificate.json": {
                "verdict": "GRAPH_FAIL_MISSING_HANDOFF",
                "is_fail": True,
            },
        },
    }
    rec = tt.reconcile_graph_handoff(signal)
    assert rec["graph_handoff"] == "witness_overrides"
    assert not rec["contradictions"]


def test_reconcile_fail_without_witness():
    signal = {
        "gt_prebuilt_active": False,
        "hook_hash_match": None,
        "gt_meta_present": False,
        "cert_verdicts": {
            "graph_certificate.json": {
                "verdict": "GRAPH_FAIL_MISSING_HANDOFF",
                "is_fail": True,
            },
        },
    }
    rec = tt.reconcile_graph_handoff(signal)
    assert rec["graph_handoff"] == "fail"
    assert rec["contradictions"]


def test_witness_overrides_cert_fail_no_contradiction():
    """B5/B9 held-out: witness-over-cert must not leave contradictions open."""
    signal = {
        "gt_prebuilt_active": True,
        "hook_hash_match": True,
        "gt_meta_present": True,
        "cert_verdicts": {
            "graph_certificate.json": {
                "verdict": "GRAPH_FAIL_MISSING_HANDOFF",
                "is_fail": True,
            },
        },
    }
    rec = tt.reconcile_graph_handoff(signal)
    assert rec["graph_handoff"] == "witness_overrides"
    assert rec["contradictions"] == []


def test_unproven_without_meta_is_not_pass():
    """B9: absent witness must not reconcile to pass."""
    signal = {
        "gt_prebuilt_active": False,
        "hook_hash_match": None,
        "gt_meta_present": False,
        "cert_verdicts": {},
    }
    rec = tt.reconcile_graph_handoff(signal)
    assert rec["graph_handoff"] in ("fail", "unproven")


def test_build_task_truth_writes_json():
    with tempfile.TemporaryDirectory() as jobs:
        trial = os.path.join(jobs, "run", "task__abc")
        os.makedirs(os.path.join(trial, "agent"), exist_ok=True)
        result = {
            "n_agent_steps": 5,
            "verifier_result": {"rewards": {"reward": 0.0}},
            "task_name": "org/task",
            "info": {
                "submission": "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-x\n+y\n",
            },
        }
        with open(os.path.join(trial, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        truth = tt.build_task_truth(jobs, trial_log="")
        assert truth["schema"] == "gt.task_truth.v1"
        assert truth["authority"]["outcome"] == "task_truth.outcome"
        assert truth["authority"]["brief_delivery"] == "task_truth.brief_provenance"
        assert truth["instance_id"] == "task"
        assert "reconciled" in truth
        assert truth["patch_hygiene"].get("classification") == "source_fix"


def test_task_truth_includes_product_runtime_control_surface():
    with tempfile.TemporaryDirectory() as jobs:
        trial = os.path.join(jobs, "run", "task__abc")
        agent = os.path.join(trial, "agent")
        os.makedirs(agent, exist_ok=True)
        runtime_ledger = os.path.join(trial, "gt_runtime_ledger.jsonl")
        with open(os.path.join(trial, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "n_agent_steps": 4,
                    "verifier_result": {"rewards": {"reward": 0.0}},
                    "task_name": "org/task",
                    "info": {"submission": ""},
                },
                fh,
            )
        with open(runtime_ledger, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "layer": "spec.obligation",
                "event_type": "post_view",
                "file_path": "src/app.py",
                "outcome": "suppressed_wrong_phase",
                "reason": "wrong_phase",
                "chars_delivered": 0,
                "timestamp_ms": 1,
                "iteration": 2,
            }) + "\n")
        with open(os.path.join(agent, "mini-swe-agent.trajectory.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "messages": [
                        {"action": {"command": "sed -n '1,40p' src/app.py"}},
                        {"action": {"command": "python -c \"open('src/app.py','w').write('def capture_snapshot(): pass')\""}},
                        {"action": {"command": "pytest tests/test_app.py"}, "observation": "1 passed"},
                    ]
                },
                fh,
            )
        truth = tt.build_task_truth(jobs, trial_log="[GT_META] gt_prebuilt_active=True hook_hash_match=True")
        runtime = truth["runtime_control"]
        assert runtime["phase_policy_version"].startswith("gt.runtime.context_policy.")
        assert runtime["trajectory_state_summary"]["edited_files"] == ["src/app.py"]
        assert runtime["trajectory_state_summary"]["test_evidence_seen"] is True
        assert runtime["obligation_lifecycle_summary"]["version"].startswith("gt.runtime.obligations.")
        assert runtime["verification_horizon_summary"]["version"].startswith("gt.runtime.verification_horizon.")
        assert runtime["runtime_ledger_summary"]["outcome_counts"]["suppressed_wrong_phase"] == 1
        assert runtime["enforcement_semantics"]["official_verifier_repair"] is False
        assert runtime["adapter_witness"]["gt_meta_present"] is True


def test_task_truth_prefers_chronological_source_write_receipts(tmp_path, monkeypatch):
    """Exact byte receipts outrank lossy command-shape edit inference."""
    task = "org__project-1"
    trial = tmp_path / "run" / f"{task}__attempt"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "n_agent_steps": 4,
        "verifier_result": {"rewards": {"reward": 0.0}},
        "task_name": "org/project",
        "info": {"submission": ""},
    }), encoding="utf-8")
    # Command inference sees two writes to the wrong file.  Producer receipts
    # prove that only one byte-changing write landed, in a .pyi source file.
    (agent / "mini-swe-agent.trajectory.json").write_text(json.dumps({"messages": [
        {"action": {"command": "python -c \"open('src/wrong.py','w').write('x')\""}},
        {"action": {"command": "python -c \"open('src/wrong.py','w').write('y')\""}},
    ]}), encoding="utf-8")
    ledger = trial / "gt_runtime_ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in [
        {
            "layer": "ss.coherence.proof", "event_type": "source_write_proof",
            "outcome": "suppressed_internal_only",
            "reason": "exact_post_command_write_result", "chars_delivered": 0,
            "action_step": 1, "iteration": 1, "file_path": "src/api.pyi",
            "write_ok": True, "bytes_changed": False,
        },
        {
            "layer": "ss.coherence.proof", "event_type": "source_write_proof",
            "outcome": "suppressed_internal_only",
            "reason": "exact_post_command_write_result", "chars_delivered": 0,
            "action_step": 2, "iteration": 2, "file_path": "src/api.pyi",
            "write_ok": True, "bytes_changed": True,
        },
    ]) + "\n", encoding="utf-8")
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))

    truth = tt.build_task_truth(str(tmp_path), instance_id=task)
    state = truth["trajectory_state"]

    assert state["edit_truth_authority"] == "ss.coherence.proof/source_write_proof"
    assert state["source_write_proof_count"] == 2
    assert state["edited_files"] == ["src/api.pyi"]
    assert state["source_edit_count"] == 1


def test_task_truth_falls_back_only_when_source_write_receipts_are_absent(
    tmp_path, monkeypatch,
):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": [
        {"action": {"command": "python -c \"open('src/inferred.py','w').write('x')\""}},
    ]}), encoding="utf-8")
    monkeypatch.delenv("GT_RUNTIME_LEDGER", raising=False)

    summary = tt._trajectory_state_summary({
        "mini_trajectory": str(trajectory), "runtime_ledger": None,
    })

    assert summary["edit_truth_authority"] == "trajectory_command_inference"
    assert summary["source_write_proof_count"] == 0
    assert summary["edited_files"] == ["src/inferred.py"]
    assert summary["source_edit_count"] == 1


def test_task_truth_does_not_fallback_over_malformed_source_write_receipt(
    tmp_path,
):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": [
        {"action": {"command": "python -c \"open('src/inferred.py','w').write('x')\""}},
    ]}), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({
        "layer": "ss.coherence.proof", "event_type": "source_write_proof",
        "outcome": "suppressed_internal_only",
        "reason": "exact_post_command_write_result", "chars_delivered": 0,
        "action_step": 1, "iteration": 1, "file_path": "../escape.py",
        "write_ok": True, "bytes_changed": True,
    }) + "\n", encoding="utf-8")

    summary = tt._trajectory_state_summary({
        "mini_trajectory": str(trajectory), "runtime_ledger": str(ledger),
    })

    assert summary["edit_truth_authority"].startswith("invalid_ss.coherence.proof")
    assert summary["edit_truth_valid"] is False
    assert summary["source_edit_count"] is None
    assert summary["edited_files"] == []


def test_task_truth_resets_write_window_after_passing_test_receipt(tmp_path):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": []}), encoding="utf-8")
    common = {
        "layer": "ss.coherence.proof", "outcome": "suppressed_internal_only",
        "chars_delivered": 0,
    }
    rows = [
        {**common, "event_type": "source_write_proof",
         "reason": "exact_post_command_write_result", "action_step": 1,
         "iteration": 1, "file_path": "src/before.py", "write_ok": True,
         "bytes_changed": True},
        {**common, "event_type": "test_proof", "reason": "classified_test_result",
         "action_step": 2, "iteration": 2, "file_path": "", "passed": True},
        {**common, "event_type": "source_write_proof",
         "reason": "exact_post_command_write_result", "action_step": 3,
         "iteration": 3, "file_path": "src/after.py", "write_ok": True,
         "bytes_changed": False},
        {**common, "event_type": "source_write_proof",
         "reason": "exact_post_command_write_result", "action_step": 4,
         "iteration": 4, "file_path": "src/after.py", "write_ok": True,
         "bytes_changed": True},
    ]
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary = tt._trajectory_state_summary({
        "mini_trajectory": str(trajectory), "runtime_ledger": str(ledger),
    })

    assert summary["edit_truth_valid"] is True
    assert summary["source_write_proof_count"] == 3
    assert summary["test_proof_count"] == 1
    assert summary["latest_passing_test_step"] == 2
    assert summary["source_edit_count"] == 2
    assert summary["edited_files"] == ["src/after.py", "src/before.py"]
    assert summary["coherence_write_count"] == 1
    assert summary["coherence_edited_files"] == ["src/after.py"]


def test_task_truth_fails_closed_on_malformed_test_proof(tmp_path):
    common = {
        "layer": "ss.coherence.proof", "outcome": "suppressed_internal_only",
        "chars_delivered": 0,
    }
    rows = [
        {**common, "event_type": "source_write_proof",
         "reason": "exact_post_command_write_result", "action_step": 1,
         "iteration": 1, "file_path": "src/app.py", "write_ok": True,
         "bytes_changed": True},
        {**common, "event_type": "test_proof", "reason": "classified_test_result",
         "action_step": 2, "iteration": 2, "file_path": "", "passed": "yes"},
    ]
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary = tt._trajectory_state_summary({
        "mini_trajectory": None, "runtime_ledger": str(ledger),
    })

    assert summary["edit_truth_valid"] is False
    assert summary["source_edit_count"] is None
    assert summary["edited_files"] == []


def test_final_passing_test_preserves_whole_task_edits(tmp_path):
    common = {
        "layer": "ss.coherence.proof", "outcome": "suppressed_internal_only",
        "chars_delivered": 0,
    }
    rows = [
        {**common, "event_type": "source_write_proof",
         "reason": "exact_post_command_write_result", "action_step": 1,
         "iteration": 1, "file_path": "src/app.py", "write_ok": True,
         "bytes_changed": True},
        {**common, "event_type": "test_proof", "reason": "classified_test_result",
         "action_step": 2, "iteration": 2, "file_path": "", "passed": True},
    ]
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary = tt._trajectory_state_summary({
        "mini_trajectory": None, "runtime_ledger": str(ledger),
    })

    assert summary["source_edit_count"] == 1
    assert summary["edited_files"] == ["src/app.py"]
    assert summary["coherence_write_count"] == 0
    assert summary["coherence_edited_files"] == []


def test_native_mini_messages_pair_tool_calls_without_scanning_instructions(tmp_path):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"

    def assistant(call_id: str, command: str) -> dict:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": command}),
                },
            }],
        }

    trajectory.write_text(json.dumps({"messages": [
        {"role": "system", "content": "Available tags include <gt-nudge>."},
        {"role": "user", "content": "Template mentions <gt-contract> but sends neither."},
        assistant("view", "sed -n '1,40p' src/app.py"),
        {"role": "tool", "tool_call_id": "view", "content": "def old(): pass"},
        assistant("edit", "python -c \"open('src/app.py','w').write('def new(): pass')\""),
        {"role": "tool", "tool_call_id": "edit", "content": "Done\n<gt-evidence>verified</gt-evidence>"},
        assistant("test", "pytest tests/test_app.py"),
        {"role": "tool", "tool_call_id": "test", "content": "1 passed"},
    ]}), encoding="utf-8")

    turns = tt._turns_from_mini_trajectory(str(trajectory))
    state = tt.derive_state(turns)

    assert len(turns) == 3
    assert state.viewed_files == {"src/app.py"}
    assert state.edited_files == {"src/app.py"}
    assert state.source_edit_count == 1
    assert state.test_count == 1
    assert state.test_evidence_seen is True
    assert state.delivered_markers == [(2, "gt-evidence", "")]


def test_native_zero_action_messages_never_scan_instruction_markers(tmp_path):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": [
        {"role": "system", "content": "Available tag <gt-nudge>."},
        {"role": "user", "content": "Template tag <gt-contract>."},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]}), encoding="utf-8")

    turns = tt._turns_from_mini_trajectory(str(trajectory))

    assert turns == []
    assert tt.derive_state(turns).delivered_markers == []


def test_native_terminal_exit_completes_the_pending_action(tmp_path):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": [
        {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": "submit", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                })},
            }],
        },
        {"role": "exit", "content": "submitted"},
    ]}), encoding="utf-8")

    turns = tt._turns_from_mini_trajectory(str(trajectory))

    assert len(turns) == 1
    assert turns[0].command == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    assert turns[0].observation == "submitted"


def test_native_terminal_exit_after_matched_result_is_framing_only(tmp_path):
    """Older trajectories record LimitsExceeded after the final result."""
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": [
        {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": "last", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": "pytest -q",
                })},
            }],
        },
        {"role": "tool", "tool_call_id": "last", "content": "1 passed"},
        {"role": "exit", "content": "LimitsExceeded"},
    ]}), encoding="utf-8")

    turns = tt._turns_from_mini_trajectory(str(trajectory))

    assert len(turns) == 1
    assert turns[0].command == "pytest -q"
    assert turns[0].observation == "1 passed"


def test_native_reused_resolved_tool_call_id_fails_closed(tmp_path):
    call = lambda command: {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "reused", "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({
                "command": command,
            })},
        }],
    }
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": [
        call("first"),
        {"role": "tool", "tool_call_id": "reused", "content": "one"},
        call("second"),
        {"role": "tool", "tool_call_id": "reused", "content": "two"},
    ]}), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate tool-call id"):
        tt._turns_from_mini_trajectory(str(trajectory))


@pytest.mark.parametrize("messages", [
    [{"role": "assistant", "tool_calls": []}, "corrupt-record"],
    [{"role": "assistant", "tool_calls": {"id": "not-a-list"}}],
    [{"role": "unsupported", "content": "not native schema"}],
])
def test_native_malformed_message_containers_fail_closed(tmp_path, messages):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": messages}), encoding="utf-8")

    with pytest.raises(ValueError, match="native trajectory"):
        tt._turns_from_mini_trajectory(str(trajectory))


def test_native_malformed_json_document_fails_closed(tmp_path):
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="native trajectory document"):
        tt._turns_from_mini_trajectory(str(trajectory))


@pytest.mark.parametrize("failure", ["duplicate", "malformed", "unmatched"])
def test_native_malformed_pairing_fails_closed(tmp_path, failure):
    call = {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "same", "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({
                "command": "sed -n '1,2p' src/app.py",
            })},
        }],
    }
    messages = [call]
    if failure == "duplicate":
        messages.extend([
            call,
            {"role": "tool", "tool_call_id": "same", "content": "x"},
        ])
    elif failure == "malformed":
        messages[0]["tool_calls"][0]["function"]["arguments"] = "{"
    else:
        messages = [{"role": "tool", "tool_call_id": "missing", "content": "x"}]
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(json.dumps({"messages": messages}), encoding="utf-8")

    with pytest.raises(ValueError, match="native trajectory"):
        tt._turns_from_mini_trajectory(str(trajectory))


def test_reconciled_substrate_verdict_shape():
    truth = {
        "instance_id": "abs-module-cache-flags",
        "certs": {
            "graph_certificate.json": {"verdict": "GRAPH_OK"},
            "lsp_certificate.json": {"verdict": "LSP_OK"},
        },
        "reconciled": {"graph_handoff": "pass", "witness_holds": True, "contradictions": []},
        "outcome": {"failure_class": "AGENT", "in_resolved_denominator": True},
    }
    verdict = tt.build_reconciled_substrate_verdict(truth)
    assert verdict["schema"] == "gt.reconciled_substrate_verdict.v1"
    assert verdict["authority"] == "task_truth.json"
    assert verdict["authority_map"]["substrate"] == "reconciled_substrate_verdict.json"
    assert verdict["graph_handoff"] == "pass"


def test_write_task_truth_emits_reconciled_substrate_verdict():
    with tempfile.TemporaryDirectory() as jobs:
        trial = os.path.join(jobs, "run", "task__abc")
        os.makedirs(os.path.join(trial, "agent"), exist_ok=True)
        with open(os.path.join(trial, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "n_agent_steps": 1,
                    "verifier_result": {"rewards": {"reward": 0.0}},
                    "task_name": "org/task",
                },
                fh,
            )
        out = tt.write_task_truth(jobs)
        assert os.path.isfile(out)
        rec_path = os.path.join(os.path.dirname(out), "reconciled_substrate_verdict.json")
        assert os.path.isfile(rec_path)
