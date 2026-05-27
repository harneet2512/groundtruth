"""Tests for gt_verify_topology.py — the topology pass/fail judge."""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from gt_verify_topology import verify_topology


def make_autopsy(layers: dict, hidden_prefix_leaks: list | None = None) -> dict:
    """Build a minimal autopsy dict for testing."""
    return {
        "task_id": "test_task",
        "directory": "/tmp/test",
        "total_history_entries": 100,
        "total_agent_actions": 50,
        "total_gt_injections": 5,
        "layers": layers,
        "divergences": [],
        "candidate_bugs": [],
        "hidden_prefix_leaks": hidden_prefix_leaks or [],
    }


class TestG1GeneratedButNotVisible:
    """G1: gt_layer_events says delivered but output.jsonl lacks evidence."""

    def test_g1_fails_when_generated_not_visible(self):
        autopsy = make_autopsy({
            "L3_POST_EDIT": {
                "expected": "yes",
                "generated": True,
                "visible_in_output": False,
                "status": "LOST",
                "failure_class": "G1",
                "markers_found": [],
                "events_from_log": 3,
            }
        })
        result = verify_topology(autopsy)
        assert not result["overall_pass"]
        assert result["fail_count"] >= 1
        g1_verdicts = [v for v in result["verdicts"] if v["failure_class"] == "G1"]
        assert len(g1_verdicts) == 1
        assert g1_verdicts[0]["layer"] == "L3_POST_EDIT"

    def test_generated_and_visible_passes(self):
        autopsy = make_autopsy({
            "L3_POST_EDIT": {
                "expected": "yes",
                "generated": True,
                "visible_in_output": True,
                "status": "DELIVERED",
                "failure_class": "",
                "markers_found": ["[SIGNATURE]"],
                "events_from_log": 3,
            }
        })
        result = verify_topology(autopsy)
        assert result["overall_pass"]
        assert result["fail_count"] == 0


class TestC6BaselineContainsGT:
    """C6: baseline arm contains GT evidence."""

    def test_c6_fails_when_baseline_has_gt(self):
        autopsy = make_autopsy({
            "L1_BRIEF": {
                "expected": "yes",
                "generated": False,
                "visible_in_output": True,
                "status": "DELIVERED",
                "failure_class": "",
                "markers_found": ["<gt-task-brief>"],
                "events_from_log": 0,
            }
        })
        result = verify_topology(autopsy, is_baseline=True)
        assert not result["overall_pass"]
        c6_verdicts = [v for v in result["verdicts"] if v["failure_class"] == "C6"]
        assert len(c6_verdicts) == 1

    def test_baseline_without_gt_passes(self):
        autopsy = make_autopsy({
            "L1_BRIEF": {
                "expected": "yes",
                "generated": False,
                "visible_in_output": False,
                "status": "NOT_FIRED",
                "failure_class": "",
                "markers_found": [],
                "events_from_log": 0,
            }
        })
        result = verify_topology(autopsy, is_baseline=True)
        assert result["overall_pass"]


class TestF2FinishEvidenceTooLate:
    """F2: L6/pre-submit evidence after agent finished."""

    def test_f2_l6_generated_not_visible(self):
        autopsy = make_autopsy({
            "L6_PRESUBMIT": {
                "expected": "broken(OH)",
                "generated": True,
                "visible_in_output": False,
                "status": "LOST",
                "failure_class": "F2",
                "markers_found": [],
                "events_from_log": 1,
            }
        })
        result = verify_topology(autopsy)
        assert not result["overall_pass"]
        f2_verdicts = [v for v in result["verdicts"] if v["failure_class"] == "F2"]
        assert len(f2_verdicts) == 1


class TestE3HiddenPrefixLeaks:
    """E3: hidden prefixes leak into agent observations."""

    def test_e3_fails_on_leaks(self):
        autopsy = make_autopsy(
            {"L1_BRIEF": {
                "expected": "yes", "generated": True, "visible_in_output": True,
                "status": "DELIVERED", "failure_class": "", "markers_found": [],
                "events_from_log": 1,
            }},
            hidden_prefix_leaks=[
                {"entry_idx": 5, "prefixes": ["[GT_META]"], "context": "..."},
            ],
        )
        result = verify_topology(autopsy)
        assert not result["overall_pass"]
        e3_verdicts = [v for v in result["verdicts"] if v["failure_class"] == "E3"]
        assert len(e3_verdicts) == 1


class TestD1ExpectedNeverFired:
    """D1: expected layer never fired — WARN not FAIL."""

    def test_d1_is_warn_not_fail(self):
        autopsy = make_autopsy({
            "L4A_AUTO_QUERY": {
                "expected": "yes",
                "generated": False,
                "visible_in_output": False,
                "status": "NOT_FIRED",
                "failure_class": "D1",
                "markers_found": [],
                "events_from_log": 0,
            }
        })
        result = verify_topology(autopsy)
        # D1 is a warning, not a failure — overall should still pass
        assert result["overall_pass"]
        assert result["warn_count"] >= 1


class TestAllLayersPass:
    """Fully healthy autopsy passes topology."""

    def test_healthy_autopsy(self):
        layers = {}
        for key in ["L1_BRIEF", "L1_EDIT_TARGET", "L3_POST_EDIT", "L3B_POST_VIEW", "L4A_AUTO_QUERY"]:
            layers[key] = {
                "expected": "yes",
                "generated": True,
                "visible_in_output": True,
                "status": "DELIVERED",
                "failure_class": "",
                "markers_found": ["marker"],
                "events_from_log": 1,
            }
        layers["L5B_REMINDER"] = {
            "expected": "suppressed",
            "generated": False,
            "visible_in_output": False,
            "status": "NOT_FIRED",
            "failure_class": "",
            "markers_found": [],
            "events_from_log": 0,
        }
        autopsy = make_autopsy(layers)
        result = verify_topology(autopsy)
        assert result["overall_pass"]
        assert result["fail_count"] == 0
