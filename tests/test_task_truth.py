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
        assert truth["instance_id"] == "task"
        assert "reconciled" in truth
        assert truth["patch_hygiene"].get("classification") == "source_fix"


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
