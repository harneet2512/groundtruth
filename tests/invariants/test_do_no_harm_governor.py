"""Do-no-harm governor tests — correct silence and suppression decisions."""
from __future__ import annotations

from groundtruth.safety.governor import (
    DeliveryDecision,
    DELIVERED_VISIBLE,
    SUPPRESSED_NO_EDITED_FUNCTION,
    SUPPRESSED_NOISE_RISK,
    SUPPRESSED_NOT_ACTIONABLE,
    SUPPRESSED_NO_EVIDENCE,
    should_suppress_completeness,
    should_suppress_l5,
    classify_finish_delivery,
    should_emit_l4a,
)


class TestCompletenessDecisions:
    def test_empty_set_suppresses(self):
        d = should_suppress_completeness(edited_functions=set())
        assert d.status == SUPPRESSED_NO_EDITED_FUNCTION
        assert not d.should_deliver

    def test_known_function_allows(self):
        d = should_suppress_completeness(edited_functions={"set_fields"})
        assert d.status == DELIVERED_VISIBLE
        assert d.should_deliver

    def test_none_allows_legacy(self):
        d = should_suppress_completeness(edited_functions=None)
        assert d.status == DELIVERED_VISIBLE


class TestL5Decisions:
    def test_new_target_allowed(self):
        d = should_suppress_l5("file_a.py", [])
        assert d.should_deliver

    def test_repeated_target_suppressed(self):
        d = should_suppress_l5("file_a.py", ["file_a.py", "file_b.py"])
        assert d.status == SUPPRESSED_NOISE_RISK

    def test_same_target_outside_lookback_allowed(self):
        recent = ["file_a.py"] + ["other.py"] * 6
        d = should_suppress_l5("file_a.py", recent, lookback=5)
        assert d.should_deliver

    def test_different_target_allowed(self):
        d = should_suppress_l5("file_b.py", ["file_a.py", "file_c.py"])
        assert d.should_deliver


class TestFinishDelivery:
    def test_finish_is_not_actionable(self):
        d = classify_finish_delivery("L6")
        assert d.status == SUPPRESSED_NOT_ACTIONABLE
        assert not d.should_deliver


class TestL4aDecisions:
    def test_issue_keyword_allows(self):
        d = should_emit_l4a("set_fields", 2, False, True)
        assert d.should_deliver

    def test_l1_candidate_file_allows(self):
        d = should_emit_l4a("some_func", 5, True, False)
        assert d.should_deliver

    def test_structural_relevance_allows(self):
        d = should_emit_l4a("helper", 3, False, False)
        assert d.should_deliver

    def test_high_degree_hub_without_relevance_suppressed(self):
        d = should_emit_l4a("init", 100, False, False)
        assert d.status == SUPPRESSED_NOISE_RISK
        assert not d.should_deliver

    def test_no_callers_no_relevance_suppressed(self):
        d = should_emit_l4a("orphan", 0, False, False)
        assert d.status == SUPPRESSED_NO_EVIDENCE

    def test_hub_with_issue_keyword_allowed(self):
        d = should_emit_l4a("Session", 246, False, True)
        assert d.should_deliver

    def test_hub_with_l1_candidate_allowed(self):
        d = should_emit_l4a("Session", 246, True, False)
        assert d.should_deliver


class TestCorrectSilence:
    """Silence is success when evidence is weak."""

    def test_no_evidence_is_correct_silence(self):
        d = should_emit_l4a("orphan", 0, False, False)
        assert d.status == SUPPRESSED_NO_EVIDENCE
        # This is correct behavior, not a failure

    def test_unknown_edited_function_is_correct_silence(self):
        d = should_suppress_completeness(set())
        assert d.status == SUPPRESSED_NO_EDITED_FUNCTION
        # Suppressing noisy class-wide completeness is correct
