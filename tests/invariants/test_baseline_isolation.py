"""Invariant 10: Baseline Isolation

When GT_BASELINE=1, the agent must receive zero GT evidence markers.
Baseline arm is the control group.

Violation = C6 in failure taxonomy.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from gt_verify_topology import verify_topology


GT_EVIDENCE_MARKERS = [
    "<gt-task-brief>", "<gt-edit-target>", "<gt-orientation>",
    "[GT_AUTO]", "[SIGNATURE]", "[BEHAVIORAL CONTRACT]",
    "PRESERVE:", "[CALLERS]", "Called by:", "Calls into:",
    "[TEST]", "[COMPLETENESS]", "[PATTERN]", "[PEER]",
    "[SIMILAR]", "[OVERRIDE]", "[MISMATCH]", "[REVIEW]",
    "<gt-advisory", "<gt-scope", "[GT L5:",
    "[GT] Callers of", "SEMANTIC WARNING:",
    "[GT KEY CONTRACTS]",
]


class TestBaselineIsolation:
    """Baseline arm must have zero GT evidence."""

    def test_baseline_with_visible_evidence_fails_topology(self):
        """If baseline autopsy shows visible GT evidence, topology must FAIL."""
        autopsy = {
            "task_id": "baseline_test",
            "layers": {
                "L1_BRIEF": {
                    "expected": "yes",
                    "generated": False,
                    "visible_in_output": True,  # GT evidence in baseline!
                    "status": "DELIVERED",
                    "failure_class": "",
                    "markers_found": ["<gt-task-brief>"],
                    "events_from_log": 0,
                }
            },
            "hidden_prefix_leaks": [],
        }
        result = verify_topology(autopsy, is_baseline=True)
        assert not result["overall_pass"], (
            "Invariant 10: baseline with GT evidence must FAIL topology"
        )
        c6 = [v for v in result["verdicts"] if v["failure_class"] == "C6"]
        assert len(c6) == 1

    def test_baseline_without_evidence_passes(self):
        """Clean baseline with no GT evidence passes topology."""
        autopsy = {
            "task_id": "baseline_clean",
            "layers": {
                "L1_BRIEF": {
                    "expected": "yes",
                    "generated": False,
                    "visible_in_output": False,
                    "status": "NOT_FIRED",
                    "failure_class": "",
                    "markers_found": [],
                    "events_from_log": 0,
                }
            },
            "hidden_prefix_leaks": [],
        }
        result = verify_topology(autopsy, is_baseline=True)
        assert result["overall_pass"]

    def test_all_gt_markers_defined(self):
        """Verify we have a comprehensive marker list for baseline checking."""
        assert len(GT_EVIDENCE_MARKERS) >= 20, (
            "Marker list should cover all GT evidence types"
        )
        # Key markers must be present
        assert "<gt-task-brief>" in GT_EVIDENCE_MARKERS
        assert "[SIGNATURE]" in GT_EVIDENCE_MARKERS
        assert "Called by:" in GT_EVIDENCE_MARKERS
        assert "[REVIEW]" in GT_EVIDENCE_MARKERS
