"""Tests for grounding-gap event log instrumentation."""

from __future__ import annotations

from groundtruth.grounding.events import (
    EventKind,
    EventLog,
    GroundingEvent,
    _hash_content,
    consumption_event,
    intervention_event,
    outcome_event,
)


class TestEventCreation:
    def test_intervention_event_fields(self) -> None:
        ev = intervention_event("s1", "check-diff", "some response", "obligation", 3)
        assert ev.kind == EventKind.INTERVENTION
        assert ev.session_id == "s1"
        assert ev.tool_name == "check-diff"
        assert ev.metadata["intervention_type"] == "obligation"
        assert ev.metadata["findings_count"] == 3
        assert isinstance(ev.timestamp, float)
        assert len(ev.content_hash) == 64  # sha256 hex

    def test_consumption_event_fields(self) -> None:
        ev = consumption_event("s1", "check-diff", "abc123", was_followed=True)
        assert ev.kind == EventKind.CONSUMPTION
        assert ev.content_hash == "abc123"
        assert ev.metadata["was_followed"] is True

    def test_consumption_event_unknown_followed(self) -> None:
        ev = consumption_event("s1", "check-diff", "abc123", was_followed=None)
        assert "was_followed" not in ev.metadata

    def test_outcome_event_fields(self) -> None:
        ev = outcome_event("s1", "check-diff", "abc123", "correct")
        assert ev.kind == EventKind.OUTCOME
        assert ev.metadata["outcome"] == "correct"


class TestContentHash:
    def test_deterministic_for_same_content(self) -> None:
        assert _hash_content("hello world") == _hash_content("hello world")

    def test_differs_for_different_content(self) -> None:
        assert _hash_content("hello") != _hash_content("world")

    def test_truncates_to_500_chars(self) -> None:
        long_a = "a" * 500 + "EXTRA"
        long_b = "a" * 500 + "DIFFERENT"
        assert _hash_content(long_a) == _hash_content(long_b)


class TestEventLog:
    def test_empty_log(self) -> None:
        log = EventLog()
        assert log.to_dicts() == []
        assert log.get_session_events("s1") == []

    def test_append_and_retrieve(self) -> None:
        log = EventLog()
        ev = intervention_event("s1", "tool", "content", "obligation", 1)
        log.append(ev)
        assert len(log.to_dicts()) == 1
        assert log.get_session_events("s1") == [ev]

    def test_get_session_events_filters_by_session(self) -> None:
        log = EventLog()
        ev1 = intervention_event("s1", "tool", "c1", "obligation", 1)
        ev2 = intervention_event("s2", "tool", "c2", "obligation", 2)
        ev3 = outcome_event("s1", "tool", "h1", "correct")
        log.append(ev1)
        log.append(ev2)
        log.append(ev3)
        s1_events = log.get_session_events("s1")
        assert len(s1_events) == 2
        assert ev2 not in s1_events

    def test_surfacing_rate_two_interventions_one_consumed(self) -> None:
        log = EventLog()
        ev1 = intervention_event("s1", "tool", "content_a", "obligation", 1)
        ev2 = intervention_event("s1", "tool", "content_b", "contradiction", 2)
        log.append(ev1)
        log.append(ev2)
        # Only consume the first
        log.append(consumption_event("s1", "tool", ev1.content_hash, was_followed=True))
        assert log.get_surfacing_rate("s1") == 0.5

    def test_surfacing_rate_no_interventions(self) -> None:
        log = EventLog()
        assert log.get_surfacing_rate("s1") == 0.0

    def test_to_dicts_serialization(self) -> None:
        log = EventLog()
        ev = intervention_event("s1", "check-diff", "resp", "obligation", 5)
        log.append(ev)
        dicts = log.to_dicts()
        assert len(dicts) == 1
        d = dicts[0]
        assert d["kind"] == "intervention"
        assert d["session_id"] == "s1"
        assert d["tool_name"] == "check-diff"
        assert d["metadata"]["findings_count"] == 5
        assert isinstance(d["content_hash"], str)
