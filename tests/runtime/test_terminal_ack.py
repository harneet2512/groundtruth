from __future__ import annotations

from dataclasses import replace

import pytest

from groundtruth.runtime.control_participation import participation_to_dict
from groundtruth.runtime.terminal_ack import (
    TerminalAckIdentity,
    build_ack_participation,
)


def _identity() -> TerminalAckIdentity:
    return TerminalAckIdentity(
        candidate_text="\ncheck src/widget.py before submit",
        fact_class="submit_refusal",
        candidate_id="candidate:submit:1",
        delivered_iteration=7,
    )


def test_ack_participation_binds_original_terminal_candidate() -> None:
    identity = _identity()
    record = build_ack_participation(
        identity, acknowledgment_iteration=9, acknowledged=True)
    payload = participation_to_dict(record)

    assert payload["control_ref"]["feature_id"] == "GT_SS_ACK_METRICS"
    assert payload["decision_site"] == "mini_seam.acknowledgment.receipt_grading"
    assert payload["decision"] == "APPLIED"
    assert payload["iteration"] == 9
    assert payload["candidate_chars"] == len(identity.candidate_text)
    assert payload["candidate_sha256_16"] == identity.candidate_sha256_16
    assert payload["fact_class"] == "submit_refusal"
    assert payload["candidate_id"] == "candidate:submit:1"
    assert payload["temporal_relation"] == "RECEIPT_FOLLOWS_DELIVERY"
    assert payload["related_delivery_iteration"] == 7
    assert "causal" not in repr(payload).lower()


def test_expired_watch_is_no_effect_not_ack_or_causality() -> None:
    record = build_ack_participation(
        _identity(), acknowledgment_iteration=14, acknowledged=False)

    assert record.decision == "NO_EFFECT"
    assert record.reason == "receipt_window_expired_without_acknowledgment"


@pytest.mark.parametrize(
    "identity,match",
    [
        (replace(_identity(), candidate_text=""), "candidate_text"),
        (replace(_identity(), fact_class="not_registered"), "fact_class"),
        (replace(_identity(), candidate_id=""), "candidate_id"),
        (replace(_identity(), delivered_iteration=True), "delivered_iteration"),
        (replace(_identity(), delivered_iteration=-1), "delivered_iteration"),
    ],
)
def test_ack_identity_fails_closed(identity: TerminalAckIdentity, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build_ack_participation(identity, acknowledgment_iteration=9, acknowledged=True)


@pytest.mark.parametrize("iteration", [True, -1, 7])
def test_ack_requires_a_strictly_later_assistant_iteration(iteration: object) -> None:
    with pytest.raises(ValueError, match="acknowledgment_iteration"):
        build_ack_participation(
            _identity(), acknowledgment_iteration=iteration, acknowledged=True)


def test_acknowledged_is_strict_bool() -> None:
    with pytest.raises(TypeError, match="acknowledged"):
        build_ack_participation(
            _identity(), acknowledgment_iteration=9, acknowledged=1)  # type: ignore[arg-type]
