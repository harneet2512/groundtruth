"""Provider-backed evidence delivery lifecycle — RED contract tests.

These tests deliberately target the canonical public API in
``groundtruth.runtime.reasoning_runtime``.  They pin the distinction between
local construction, transport progress, provider-confirmed inference, and
trajectory commitment:

    COMPILED/JOINED/DISPATCHED/PROVIDER_ACCEPTED != DELIVERED

Only a valid provider-terminal record for the same model call and the exact
joined capsule may advance an immutable delivery attempt to ``DELIVERED``.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from groundtruth.runtime.reasoning_runtime import (
    DeliveryAttempt,
    DeliveryState,
    ModelCallAttempt,
    ProviderTerminalKind,
    advance_delivery,
    commit_response,
    is_delivered,
    record_provider_terminal,
)


CAPSULE_HASH = "a" * 64
OTHER_CAPSULE_HASH = "b" * 64
PAYLOAD_HASH = "c" * 64
OTHER_PAYLOAD_HASH = "d" * 64
RESPONSE_HASH = "e" * 64


def _selected(
    *,
    model_call_id: str = "call-12a",
    capsule_hash: str = CAPSULE_HASH,
) -> DeliveryAttempt:
    return DeliveryAttempt(
        evidence_ids=("GT-E144",),
        capsule_hash=capsule_hash,
        model_call_id=model_call_id,
    )


def _provider_accepted(
    *,
    model_call_id: str = "call-12a",
    capsule_hash: str = CAPSULE_HASH,
    provider_payload_hash: str = PAYLOAD_HASH,
    provider_response_id: str = "resp-123",
) -> DeliveryAttempt:
    attempt = _selected(
        model_call_id=model_call_id,
        capsule_hash=capsule_hash,
    )
    attempt = advance_delivery(
        attempt,
        DeliveryState.COMPILED,
        observation_id="obs-205",
    )
    attempt = advance_delivery(
        attempt,
        DeliveryState.JOINED,
        joined_capsule_hash=capsule_hash,
        provider_payload_hash=provider_payload_hash,
    )
    attempt = advance_delivery(attempt, DeliveryState.DISPATCHED)
    return advance_delivery(
        attempt,
        DeliveryState.PROVIDER_ACCEPTED,
        provider_response_id=provider_response_id,
    )


def _terminal(
    kind: ProviderTerminalKind,
    *,
    model_call_id: str = "call-12a",
    joined_capsule_hash: str = CAPSULE_HASH,
    provider_payload_hash: str = PAYLOAD_HASH,
    provider_response_id: str = "resp-123",
) -> ModelCallAttempt:
    return ModelCallAttempt(
        model_call_id=model_call_id,
        joined_capsule_hash=joined_capsule_hash,
        provider_payload_hash=provider_payload_hash,
        provider_response_id=provider_response_id,
        terminal_kind=kind,
    )


@pytest.mark.parametrize(
    "state",
    [
        DeliveryState.COMPILED,
        DeliveryState.JOINED,
        DeliveryState.DISPATCHED,
        DeliveryState.PROVIDER_ACCEPTED,
    ],
)
def test_local_and_transport_progress_never_mean_delivered(
    state: DeliveryState,
) -> None:
    """Compiler/join/transport success proves progress, not model exposure."""
    attempt = _selected()
    attempt = advance_delivery(
        attempt,
        DeliveryState.COMPILED,
        observation_id="obs-205",
    )
    if state is DeliveryState.COMPILED:
        assert not is_delivered(attempt)
        return

    attempt = advance_delivery(
        attempt,
        DeliveryState.JOINED,
        joined_capsule_hash=CAPSULE_HASH,
        provider_payload_hash=PAYLOAD_HASH,
    )
    if state is DeliveryState.JOINED:
        assert not is_delivered(attempt)
        return

    attempt = advance_delivery(attempt, DeliveryState.DISPATCHED)
    if state is DeliveryState.DISPATCHED:
        assert not is_delivered(attempt)
        return

    attempt = advance_delivery(
        attempt,
        DeliveryState.PROVIDER_ACCEPTED,
        provider_response_id="resp-123",
    )
    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    assert not is_delivered(attempt)


def test_provider_terminal_requires_the_exact_joined_capsule_hash() -> None:
    attempt = _provider_accepted()

    with pytest.raises(ValueError, match="capsule"):
        record_provider_terminal(
            attempt,
            _terminal(
                ProviderTerminalKind.COMPLETED,
                joined_capsule_hash=OTHER_CAPSULE_HASH,
            ),
        )

    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    assert not is_delivered(attempt)


def test_provider_terminal_requires_the_same_model_call_id() -> None:
    attempt = _provider_accepted()

    with pytest.raises(ValueError, match="model.call|model_call"):
        record_provider_terminal(
            attempt,
            _terminal(
                ProviderTerminalKind.COMPLETED,
                model_call_id="call-unrelated",
            ),
        )

    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    assert not is_delivered(attempt)


def test_provider_terminal_must_bind_to_the_exact_outbound_payload() -> None:
    attempt = _provider_accepted()

    with pytest.raises(ValueError, match="payload"):
        record_provider_terminal(
            attempt,
            _terminal(
                ProviderTerminalKind.COMPLETED,
                provider_payload_hash=OTHER_PAYLOAD_HASH,
            ),
        )

    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    assert not is_delivered(attempt)


@pytest.mark.parametrize(
    "terminal_kind",
    [
        ProviderTerminalKind.COMPLETED,
        ProviderTerminalKind.INCOMPLETE,
        ProviderTerminalKind.TOOL_USE,
        ProviderTerminalKind.REFUSAL,
    ],
)
def test_valid_provider_terminal_inference_counts_as_delivered(
    terminal_kind: ProviderTerminalKind,
) -> None:
    """Delivery proves inference over the payload, not answer quality."""
    accepted = _provider_accepted()

    delivered = record_provider_terminal(accepted, _terminal(terminal_kind))

    assert delivered.state is DeliveryState.DELIVERED
    assert delivered.terminal_kind is terminal_kind
    assert is_delivered(delivered)
    # Immutable transition: provider confirmation cannot rewrite its predecessor.
    assert accepted.state is DeliveryState.PROVIDER_ACCEPTED
    assert not is_delivered(accepted)


@pytest.mark.parametrize(
    "terminal_kind",
    [
        ProviderTerminalKind.FAILED,
        ProviderTerminalKind.CANCELLED,
        ProviderTerminalKind.PARTIAL_STREAM,
    ],
)
def test_failed_cancelled_or_partial_provider_result_is_not_delivered(
    terminal_kind: ProviderTerminalKind,
) -> None:
    accepted = _provider_accepted()

    result = record_provider_terminal(accepted, _terminal(terminal_kind))

    assert result.state not in {
        DeliveryState.DELIVERED,
        DeliveryState.RESPONSE_COMMITTED,
    }
    assert not is_delivered(result)
    assert accepted.state is DeliveryState.PROVIDER_ACCEPTED


def test_provider_confirmation_requires_a_provider_response_identity() -> None:
    accepted = _provider_accepted()

    with pytest.raises(ValueError, match="provider.response|provider_response"):
        record_provider_terminal(
            accepted,
            _terminal(
                ProviderTerminalKind.COMPLETED,
                provider_response_id="",
            ),
        )


def test_response_commitment_is_separate_from_delivery() -> None:
    delivered = record_provider_terminal(
        _provider_accepted(),
        _terminal(ProviderTerminalKind.COMPLETED),
    )

    assert delivered.state is DeliveryState.DELIVERED
    committed = commit_response(delivered, response_hash=RESPONSE_HASH)

    assert committed.state is DeliveryState.RESPONSE_COMMITTED
    assert is_delivered(committed)
    assert committed.response_hash == RESPONSE_HASH
    assert delivered.state is DeliveryState.DELIVERED
    assert delivered.response_hash == ""


def test_response_cannot_be_committed_before_provider_confirmed_delivery() -> None:
    accepted = _provider_accepted()

    with pytest.raises(ValueError, match="DELIVERED|delivered"):
        commit_response(accepted, response_hash=RESPONSE_HASH)

    assert accepted.state is DeliveryState.PROVIDER_ACCEPTED


def test_delivery_and_model_call_records_are_frozen_values() -> None:
    delivery = _provider_accepted()
    terminal = _terminal(ProviderTerminalKind.COMPLETED)

    with pytest.raises(FrozenInstanceError):
        delivery.state = DeliveryState.DELIVERED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        terminal.model_call_id = "tampered"  # type: ignore[misc]


def test_retry_attempts_are_independent_and_immutable() -> None:
    """A successful retry never rewrites the failed model-call attempt."""
    first = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-failed",
    )
    first_result = record_provider_terminal(
        first,
        _terminal(
            ProviderTerminalKind.FAILED,
            model_call_id="call-12a",
            provider_response_id="resp-failed",
        ),
    )

    retry = _provider_accepted(
        model_call_id="call-12b",
        provider_response_id="resp-success",
    )
    retry_result = record_provider_terminal(
        retry,
        _terminal(
            ProviderTerminalKind.COMPLETED,
            model_call_id="call-12b",
            provider_response_id="resp-success",
        ),
    )

    assert first.model_call_id == "call-12a"
    assert first.state is DeliveryState.PROVIDER_ACCEPTED
    assert not is_delivered(first_result)
    assert first_result.model_call_id == "call-12a"
    assert retry.model_call_id == "call-12b"
    assert retry.state is DeliveryState.PROVIDER_ACCEPTED
    assert is_delivered(retry_result)
    assert retry_result.model_call_id == "call-12b"


def test_delivery_cannot_skip_provider_acceptance() -> None:
    dispatched = advance_delivery(
        advance_delivery(
            advance_delivery(
                _selected(),
                DeliveryState.COMPILED,
                observation_id="obs-205",
            ),
            DeliveryState.JOINED,
            joined_capsule_hash=CAPSULE_HASH,
            provider_payload_hash=PAYLOAD_HASH,
        ),
        DeliveryState.DISPATCHED,
    )

    with pytest.raises(ValueError, match="PROVIDER_ACCEPTED|provider"):
        record_provider_terminal(
            dispatched,
            _terminal(ProviderTerminalKind.COMPLETED),
        )

    assert dispatched.state is DeliveryState.DISPATCHED
    assert not is_delivered(dispatched)


def test_terminal_states_cannot_be_forged_through_direct_construction() -> None:
    with pytest.raises(ValueError, match="terminal|proof|provider"):
        DeliveryAttempt(
            evidence_ids=("GT-E144",),
            capsule_hash=CAPSULE_HASH,
            model_call_id="call-12a",
            state=DeliveryState.DELIVERED,
        )

    with pytest.raises(ValueError, match="terminal|proof|response"):
        DeliveryAttempt(
            evidence_ids=("GT-E144",),
            capsule_hash=CAPSULE_HASH,
            model_call_id="call-12a",
            state=DeliveryState.RESPONSE_COMMITTED,
        )
