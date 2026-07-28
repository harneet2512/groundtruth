"""Task-start brief localization has its own honest evidence boundary."""

from groundtruth.runtime.fact_registry import (
    EVENT_SEARCH_RESULT,
    EVENT_TASK_START,
    producer_matches,
    registration_for,
    required_event,
)


def test_brief_and_reactive_localization_have_distinct_boundaries() -> None:
    assert registration_for("brief_localization").fact_class == "localization"
    assert required_event("brief_localization") == EVENT_TASK_START
    assert producer_matches("brief_localization", "v1r_brief")

    assert registration_for("localization").fact_class == "localization"
    assert required_event("localization") == EVENT_SEARCH_RESULT
