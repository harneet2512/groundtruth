from __future__ import annotations

from dataclasses import replace

import json
import pytest

from groundtruth.runtime.chronological_adjudication import (
    CAUSAL,
    LATE,
    EXPIRED,
    ON_TIME,
    SELF_LOCALIZED,
    STEP_BEHIND,
    UNMEASURED,
    WRONG_EVENT,
    Chronology,
    MatchedProbe,
    adjudicate,
)


def _chronology(**changes) -> Chronology:
    values = {
        "decision_open_index": 10,
        "delivery_index": 10,
        "decision_commit_index": 12,
        "native_acquisition_index": None,
        "acknowledgment_index": 11,
        "action_index": 12,
    }
    values.update(changes)
    return Chronology(**values)


def test_registry_boundary_and_chronology_prove_on_time_but_not_causality() -> None:
    result = adjudicate(
        evidence_type="syntax_result",
        actual_event="edit_result",
        delivery_seal="a" * 16,
        chronology=_chronology(),
    )

    assert result.timing_verdict == ON_TIME
    assert result.required_event == "edit_result"
    assert result.fair_probe_verdict == UNMEASURED


def test_prior_native_acquisition_is_step_behind_and_rejects_fair_probe() -> None:
    result = adjudicate(
        evidence_type="caller_break",
        actual_event="edit_result",
        delivery_seal="b" * 16,
        chronology=_chronology(native_acquisition_index=9),
    )

    assert result.timing_verdict == STEP_BEHIND
    assert result.fair_probe_verdict == SELF_LOCALIZED


def test_delivery_after_commit_is_late_even_at_named_registry_event() -> None:
    # E (logical clock): a genuine LATE delivery has no VALID post-delivery action_index (a real
    # action_index is always > delivery_index by construction), so it is None here; the default
    # action_index=12 would be < delivery=13, which E now correctly rejects as an inverted clock.
    result = adjudicate(
        evidence_type="signature_mismatch",
        actual_event="edit_result",
        delivery_seal="c" * 16,
        chronology=_chronology(delivery_index=13, action_index=None),
    )

    # RE-POINTED 2026-07-28. `LATE` was SPLIT: `delivered > committed` fused two
    # opposite outcomes -- late-but-still-correctable vs nothing-left-to-change.
    # This fixture has NO post-delivery action (action_index=None), which this
    # test's own comment already identified as the genuine-late shape, so it is
    # EXPIRED. The property is unchanged; the verdict is finer.
    assert result.timing_verdict == EXPIRED
    assert result.fair_probe_verdict == UNMEASURED


def test_delivery_at_commit_boundary_is_still_on_time() -> None:
    # E: action_index=None (no post-delivery action) — the default action_index=12 would EQUAL
    # delivery=12, which E rejects (action cannot coincide with its own delivery).
    result = adjudicate(
        evidence_type="signature_mismatch",
        actual_event="edit_result",
        delivery_seal="c" * 16,
        chronology=_chronology(delivery_index=12, action_index=None),
    )

    assert result.timing_verdict == ON_TIME


def test_wrong_event_or_missing_indices_fail_closed_unmeasured() -> None:
    # B-BND (b2): ``covering_red`` is now REACTIVE (on-time by construction at its edit
    # boundary), so it is no longer a valid "wrong-event" example — the wrong-event fail-closed
    # contract is demonstrated on a NON-reactive fixed-boundary class (``syntax_result`` wants
    # edit_result; ``file_view`` is the wrong event). The missing-index path is exercised on
    # covering_red to keep its coverage: reactive skips only the event check, never the index
    # checks, so a missing commit index still fails closed to UNMEASURED.
    wrong_event = adjudicate(
        evidence_type="syntax_result",
        actual_event="file_view",
        delivery_seal="d" * 16,
        chronology=_chronology(),
    )
    missing = adjudicate(
        evidence_type="covering_red",
        actual_event="test_result",
        delivery_seal="e" * 16,
        chronology=_chronology(decision_commit_index=None),
    )

    # C-cluster split: a KNOWN-but-wrong event for a non-reactive class is now a MEASURED
    # timing FAILURE (WRONG_EVENT -> correct_time=False), no longer conflated with UNMEASURED.
    assert wrong_event.timing_verdict == WRONG_EVENT
    # a GENUINELY-missing index still fails closed to UNMEASURED (the only UNMEASURED path).
    assert missing.timing_verdict == UNMEASURED


def test_exact_matched_probe_can_prove_causality_only_for_same_seal_and_outcome() -> None:
    probe = MatchedProbe(
        probe_id="shadow-pair-7",
        assignment_unit_id="candidate-7",
        treatment_seal="f" * 16,
        treatment_outcome="acted",
        control_outcome="not_acted",
        assignment_sha256="1" * 64,
        treatment_sha256="2" * 64,
        control_sha256="3" * 64,
        outcome_sha256="4" * 64,
    )
    proven = adjudicate(
        evidence_type="syntax_result",
        actual_event="edit_result",
        delivery_seal="f" * 16,
        chronology=_chronology(),
        matched_probe=probe,
    )
    mismatched = adjudicate(
        evidence_type="syntax_result",
        actual_event="edit_result",
        delivery_seal="0" * 16,
        chronology=_chronology(),
        matched_probe=probe,
    )

    assert proven.fair_probe_verdict == CAUSAL
    assert mismatched.fair_probe_verdict == UNMEASURED

    no_delta = adjudicate(
        evidence_type="syntax_result",
        actual_event="edit_result",
        delivery_seal="f" * 16,
        chronology=_chronology(),
        matched_probe=replace(probe, control_outcome="acted"),
    )
    assert no_delta.fair_probe_verdict == UNMEASURED


def test_ack_or_action_before_delivery_cannot_count() -> None:
    # E (logical clock): an action_index at-or-before the delivery is an inverted clock — timing
    # now fails CLOSED to UNMEASURED (an action cannot precede the delivery that caused it), and
    # the fair probe still cannot count it (never CAUSAL from a pre-delivery action).
    result = adjudicate(
        evidence_type="syntax_result",
        actual_event="edit_result",
        delivery_seal="a" * 16,
        chronology=_chronology(acknowledgment_index=9, action_index=9),
    )

    assert result.timing_verdict == UNMEASURED
    assert result.fair_probe_verdict == UNMEASURED


def test_artifacts_are_deterministic_and_bind_exact_chronology_and_seal() -> None:
    result = adjudicate(
        evidence_type="syntax_result",
        actual_event="edit_result",
        delivery_seal="a" * 16,
        chronology=_chronology(),
    )

    assert result.timing_bytes() == result.timing_bytes()
    timing = json.loads(result.timing_bytes())
    fair = json.loads(result.fair_probe_bytes())
    assert timing["delivery_seal"] == "a" * 16
    assert timing["chronology"]["delivery_index"] == 10
    assert fair["verdict"] == UNMEASURED


def test_malformed_delivery_seal_is_rejected() -> None:
    with pytest.raises(ValueError, match="delivery seal"):
        adjudicate(
            evidence_type="syntax_result",
            actual_event="edit_result",
            delivery_seal="BAD",
            chronology=_chronology(),
        )
