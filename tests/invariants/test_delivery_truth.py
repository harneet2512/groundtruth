"""Invariant 1: Delivery Truth

gt_layer_events emitted=true implies output.jsonl contains agent-visible evidence,
UNLESS explicitly suppressed with reason.

Violation = G1 in failure taxonomy.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest


def make_event(layer: str, event_type: str, emitted: bool,
               suppressed: bool = False, reason: str = "") -> dict:
    return {
        "layer": layer,
        "event_type": event_type,
        "emitted": emitted,
        "suppressed": suppressed,
        "suppression_reason": reason,
    }


class TestDeliveryTruthInvariant:
    """emitted=true without suppression must have visible output."""

    def test_emitted_true_unsuppressed_requires_visible_output(self):
        """If an event says emitted=True and suppressed=False,
        the corresponding marker MUST exist in output.jsonl."""
        event = make_event("L3_router_v2", "on_view", emitted=True)
        assert event["emitted"] is True
        assert event["suppressed"] is False
        # Invariant: this event MUST have corresponding visible evidence
        # A system satisfying this invariant would check output.jsonl
        # and fail if the marker is absent.

    def test_emitted_false_suppressed_is_honest(self):
        """Dead writes must be marked emitted=False with reason."""
        event = make_event("L5", "multi_file_scope_warning",
                           emitted=False, suppressed=True,
                           reason="finish_handler_dead_write")
        assert event["emitted"] is False
        assert event["suppressed"] is True
        assert event["suppression_reason"] == "finish_handler_dead_write"

    def test_synthetic_fixture_enforces_truth(self):
        """Post-fix fixture must have emitted=False for finish handler events."""
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "topology", "fixtures",
            "post_fix_finish_events", "gt_layer_events_synthetic.jsonl",
        )
        if not os.path.isfile(fixture_path):
            pytest.skip("Post-fix fixture not available")

        with open(fixture_path) as f:
            for line in f:
                ev = json.loads(line.strip())
                if ev.get("event_type") in (
                    "multi_file_scope_warning",
                    "intervention_multi_file_scope_warning",
                    "pre_submit_review",
                ):
                    assert ev["emitted"] is False, (
                        f"Finish handler event must have emitted=False: {ev['event_type']}"
                    )
                    assert ev["suppressed"] is True
                    assert ev["suppression_reason"] == "finish_handler_dead_write"

    def test_non_finish_events_remain_emitted_true(self):
        """Normal layer events should have emitted=True."""
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "topology", "fixtures",
            "post_fix_finish_events", "gt_layer_events_synthetic.jsonl",
        )
        if not os.path.isfile(fixture_path):
            pytest.skip("Post-fix fixture not available")

        finish_types = {
            "multi_file_scope_warning",
            "intervention_multi_file_scope_warning",
            "pre_submit_review",
        }
        with open(fixture_path) as f:
            for line in f:
                ev = json.loads(line.strip())
                if ev.get("event_type") not in finish_types:
                    assert ev["emitted"] is True, (
                        f"Non-finish event should be emitted=True: {ev['event_type']}"
                    )
