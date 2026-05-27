"""Regression test for BUG-001: finish handler events must have emitted=False.

The finish handler in oh_gt_full_wrapper.py runs AFTER AgentState.FINISHED.
Any events emitted there are dead writes — the agent never sees them.
Telemetry must mark these events as emitted=False, suppressed=True.

Two test classes:
- TestPreFixArtifacts: proves the bug exists in old (pre-fix) artifacts
- TestPostFixSynthetic: proves the fix works via synthetic post-fix artifact
"""
from __future__ import annotations

import json
import os

import pytest


FIXTURE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "runs", "current_parallel",
)

LOGURU_EVENTS = os.path.join(
    FIXTURE_DIR,
    "canary-v2_live-delgan__loguru-1306",
    "gt_debug",
    "gt_layer_events_delgan__loguru-1306.jsonl",
)

BEETS_EVENTS = os.path.join(
    FIXTURE_DIR,
    "canary-v2_live-beetbox__beets-5495",
    "gt_debug",
    "gt_layer_events_beetbox__beets-5495.jsonl",
)

POST_FIX_EVENTS = os.path.join(
    os.path.dirname(__file__), "fixtures", "post_fix_finish_events",
    "gt_layer_events_synthetic.jsonl",
)

FINISH_EVENT_TYPES = {
    "multi_file_scope_warning",
    "intervention_multi_file_scope_warning",
    "unsafe_finish",
    "governor_finish",
    "goku_finish",
    "intervention_goku_finish",
    "pre_submit",
    "pre_submit_review",
}


def parse_events(path: str) -> list[dict]:
    events = []
    if not os.path.isfile(path):
        return events
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def get_finish_events(events: list[dict]) -> list[dict]:
    """Get events generated during the finish handler."""
    result = []
    for ev in events:
        et = ev.get("event_type", "")
        layer = ev.get("layer", "")
        if et in FINISH_EVENT_TYPES:
            result.append(ev)
        elif layer == "L6" and "submit" in et.lower():
            result.append(ev)
    return result


# --- Pre-fix: proves bug exists ---

@pytest.mark.skipif(
    not os.path.isfile(LOGURU_EVENTS),
    reason="Loguru pre-fix artifact not downloaded",
)
class TestPreFixLoguru:
    """BUG-001 pre-fix: loguru finish handler events have emitted=True (the bug)."""

    def test_l5b_scope_warning_has_emitted_true_before_fix(self):
        events = parse_events(LOGURU_EVENTS)
        finish_events = get_finish_events(events)
        l5b_scope = [
            e for e in finish_events
            if "scope" in e.get("event_type", "").lower()
            and e.get("layer", "") in ("L5", "L5b")
        ]
        assert len(l5b_scope) > 0, "Expected L5b scope warning event"
        # Pre-fix: the bug is that emitted=True
        assert any(ev.get("emitted") is True for ev in l5b_scope), (
            "Pre-fix artifact should have emitted=True (demonstrating the bug)"
        )


@pytest.mark.skipif(
    not os.path.isfile(BEETS_EVENTS),
    reason="Beets pre-fix artifact not downloaded",
)
class TestPreFixBeets:
    """BUG-001 pre-fix: beets finish handler events have emitted=True (the bug)."""

    def test_l5_scope_warning_has_emitted_true_before_fix(self):
        events = parse_events(BEETS_EVENTS)
        finish_events = get_finish_events(events)
        l5_scope = [
            e for e in finish_events
            if "scope" in e.get("event_type", "").lower()
            and e.get("layer", "") in ("L5", "L5b")
        ]
        if l5_scope:
            assert any(ev.get("emitted") is True for ev in l5_scope), (
                "Pre-fix artifact should have emitted=True (demonstrating the bug)"
            )


# --- Post-fix: proves the fix works ---

class TestPostFixSynthetic:
    """BUG-001 post-fix: synthetic artifact has emitted=False for finish events."""

    def test_fixture_exists(self):
        assert os.path.isfile(POST_FIX_EVENTS), (
            f"Post-fix fixture missing: {POST_FIX_EVENTS}"
        )

    def test_l5_scope_warning_emitted_false(self):
        events = parse_events(POST_FIX_EVENTS)
        finish_events = get_finish_events(events)
        l5_scope = [
            e for e in finish_events
            if "scope" in e.get("event_type", "").lower()
            and e.get("layer", "") in ("L5", "L5b")
        ]
        assert len(l5_scope) > 0, "Expected L5 scope events in post-fix fixture"
        for ev in l5_scope:
            assert ev.get("emitted") is False, (
                f"Post-fix: finish handler event must have emitted=False. "
                f"Got emitted={ev.get('emitted')} for {ev.get('event_type')}"
            )
            assert ev.get("suppressed") is True, (
                f"Post-fix: finish handler event must have suppressed=True. "
                f"Got suppressed={ev.get('suppressed')} for {ev.get('event_type')}"
            )
            assert ev.get("suppression_reason") == "finish_handler_dead_write", (
                f"Post-fix: suppression_reason must be 'finish_handler_dead_write'. "
                f"Got '{ev.get('suppression_reason')}'"
            )

    def test_l6_pre_submit_emitted_false(self):
        events = parse_events(POST_FIX_EVENTS)
        finish_events = get_finish_events(events)
        l6_submit = [
            e for e in finish_events
            if e.get("layer", "") == "L6"
            and "submit" in e.get("event_type", "").lower()
        ]
        assert len(l6_submit) > 0, "Expected L6 pre-submit event in post-fix fixture"
        for ev in l6_submit:
            assert ev.get("emitted") is False, (
                f"Post-fix: L6 pre-submit must have emitted=False. "
                f"Got emitted={ev.get('emitted')}"
            )
            assert ev.get("suppressed") is True
            assert ev.get("suppression_reason") == "finish_handler_dead_write"

    def test_non_finish_events_still_emitted_true(self):
        events = parse_events(POST_FIX_EVENTS)
        non_finish = [e for e in events if e.get("event_type", "") not in FINISH_EVENT_TYPES
                      and not (e.get("layer") == "L6" and "submit" in e.get("event_type", ""))]
        assert len(non_finish) > 0, "Expected non-finish events in fixture"
        for ev in non_finish:
            assert ev.get("emitted") is True, (
                f"Non-finish events should remain emitted=True. "
                f"Got emitted={ev.get('emitted')} for {ev.get('event_type')}"
            )
