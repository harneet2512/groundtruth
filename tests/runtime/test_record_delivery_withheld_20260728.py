"""#30 step 3b — persisting a holdout WITHOUT laundering it through the failure recorder.

A deliberate measurement holdout is terminal but is NOT a failure. `record_delivery_failure`'s
own allow-table is failure-only and correctly excludes `WITHHELD_FOR_MEASUREMENT`, so the
withheld transition needs its own recorder rather than a copy-paste of the failure path.

The temptation is obvious and wrong: `record_delivery_failure(attempt, WITHHELD, reason=...)`
would "work" if someone widened that table. These tests pin that it must NOT, because the
moment a holdout can travel the failure path it starts appearing in failure accounting and the
release gate — a measurement arm indistinguishable from a defect.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.reasoning_runtime import DeliveryState as DS


def _compiled_attempt() -> rr.DeliveryAttempt:
    return rr.DeliveryAttempt(
        evidence_ids=("GT-E-withheld",),
        capsule_hash="c" * 64,
        model_call_id="call-withheld",
        state=DS.COMPILED,
        observation_id="obs-withheld",
    )


def test_a_compiled_capsule_can_be_recorded_withheld() -> None:
    withheld = rr.record_delivery_withheld(
        _compiled_attempt(), reason="shadow_holdout"
    )
    assert withheld.state is DS.WITHHELD_FOR_MEASUREMENT
    assert withheld.failure_reason == "shadow_holdout"
    # Identity is preserved: the capsule that was withheld stays identifiable.
    assert withheld.capsule_hash == "c" * 64
    assert withheld.model_call_id == "call-withheld"


def test_a_dispatched_capsule_can_never_be_recorded_withheld() -> None:
    """Once the bytes went out, calling it withheld would be a lie."""
    dispatched = rr.DeliveryAttempt(
        evidence_ids=("GT-E-withheld",),
        capsule_hash="c" * 64,
        model_call_id="call-withheld",
        state=DS.DISPATCHED,
        observation_id="obs-withheld",
        joined_capsule_hash="c" * 64,
        provider_payload_hash="d" * 64,
    )
    with pytest.raises(ValueError):
        rr.record_delivery_withheld(dispatched, reason="shadow_holdout")


def test_the_failure_recorder_still_refuses_a_holdout() -> None:
    """THE LAUNDERING GUARD.

    If `record_delivery_failure` ever accepts WITHHELD_FOR_MEASUREMENT, a measurement decision
    starts flowing through failure accounting and the release gate. Keep the two paths apart.
    """
    with pytest.raises(ValueError):
        rr.record_delivery_failure(
            _compiled_attempt(), DS.WITHHELD_FOR_MEASUREMENT, reason="nope"
        )


def test_withheld_is_validated_against_the_shared_transition_table() -> None:
    """Not a second hand-written edge list — it reads the one source from step 1."""
    assert DS.WITHHELD_FOR_MEASUREMENT in rr._DELIVERY_TRANSITIONS[DS.COMPILED]
    for source in rr._DELIVERY_TRANSITIONS:
        if source is DS.COMPILED:
            continue
        assert (
            DS.WITHHELD_FOR_MEASUREMENT
            not in rr._DELIVERY_TRANSITIONS[source]
        )
