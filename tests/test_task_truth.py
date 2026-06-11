"""CP006 — task_truth reconciler tests."""
import importlib.util
import json
import os
import tempfile

import pytest

_TT_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "swebench", "task_truth.py")
_spec = importlib.util.spec_from_file_location("task_truth_t", _TT_PATH)
tt = importlib.util.module_from_spec(_spec)
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
        }
        with open(os.path.join(trial, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        truth = tt.build_task_truth(jobs, trial_log="")
        assert truth["schema"] == "gt.task_truth.v1"
        assert truth["instance_id"] == "task"
        assert "reconciled" in truth
