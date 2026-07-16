from __future__ import annotations

from dataclasses import replace

import pytest

from groundtruth.runtime.completion_control import (
    CompletionRefusalIdentity,
    build_completion_cert_participation,
    submit_refusal_candidate_id,
)
from groundtruth.runtime.control_participation import participation_to_dict


FINAL_REFUSAL = (
    "pre-commit hook failed:\n"
    "check syntax............................................Failed\n"
    "commit aborted (exit 1)"
)


def _identity(**changes: object) -> CompletionRefusalIdentity:
    base = CompletionRefusalIdentity(
        final_candidate_text=FINAL_REFUSAL,
        candidate_id=submit_refusal_candidate_id(FINAL_REFUSAL),
        iteration=8,
    )
    return replace(base, **changes)


def test_rendered_certificate_participation_binds_exact_final_refusal() -> None:
    identity = _identity()

    record = build_completion_cert_participation(
        identity,
        terminal_outcome="delivered",
        completion_cert_enabled=True,
        certificate_built=True,
        certificate_rendered=True,
    )

    assert record is not None
    payload = participation_to_dict(record)
    assert payload["control_ref"] == {
        "category": "CAP",
        "feature_id": "GT_COMPLETION_CERT",
        "role": "mediator",
    }
    assert payload["decision_site"] == (
        "mini_seam.submit_gate.completion_certificate"
    )
    assert payload["decision"] == "APPLIED"
    assert payload["iteration"] == 8
    assert payload["candidate_chars"] == len(FINAL_REFUSAL)
    assert payload["candidate_sha256_16"] == identity.candidate_sha256_16
    assert payload["fact_class"] == "submit_refusal"
    assert payload["candidate_id"] == submit_refusal_candidate_id(FINAL_REFUSAL)
    assert payload["reason"] == (
        "completion_certificate_shaped_final_submit_refusal"
    )


def test_plain_refusal_is_no_effect_when_certificate_preserved_head() -> None:
    record = build_completion_cert_participation(
        _identity(),
        terminal_outcome="delivered",
        completion_cert_enabled=True,
        certificate_built=True,
        certificate_rendered=False,
    )

    assert record is not None
    assert record.decision == "NO_EFFECT"
    assert record.reason == "completion_certificate_preserved_head_plain_submit_refusal"


@pytest.mark.parametrize(
    "terminal_outcome",
    ["allow", "duplicate", "formatter_abort", "abandoned_pending"],
)
def test_non_delivery_terminal_outcomes_are_silent(terminal_outcome: str) -> None:
    assert build_completion_cert_participation(
        None,
        terminal_outcome=terminal_outcome,
        completion_cert_enabled=True,
        certificate_built=True,
        certificate_rendered=True,
    ) is None


@pytest.mark.parametrize(
    "enabled,built",
    [(False, False), (False, True), (True, False)],
)
def test_no_participation_without_executed_completion_certificate(
    enabled: bool, built: bool,
) -> None:
    assert build_completion_cert_participation(
        _identity(),
        terminal_outcome="delivered",
        completion_cert_enabled=enabled,
        certificate_built=built,
        certificate_rendered=False,
    ) is None


def test_candidate_identity_is_content_addressed_to_final_shipped_bytes() -> None:
    changed = FINAL_REFUSAL + "\n"
    assert submit_refusal_candidate_id(changed) != submit_refusal_candidate_id(
        FINAL_REFUSAL
    )

    with pytest.raises(ValueError, match="candidate_id"):
        build_completion_cert_participation(
            _identity(final_candidate_text=changed),
            terminal_outcome="delivered",
            completion_cert_enabled=True,
            certificate_built=True,
            certificate_rendered=True,
        )


@pytest.mark.parametrize(
    "identity,match",
    [
        (_identity(final_candidate_text=""), "final_candidate_text"),
        (_identity(candidate_id=""), "candidate_id"),
        (_identity(candidate_id="not-canonical"), "candidate_id"),
        (_identity(iteration=True), "iteration"),
        (_identity(iteration=-1), "iteration"),
    ],
)
def test_delivered_identity_fails_closed(
    identity: CompletionRefusalIdentity, match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        build_completion_cert_participation(
            identity,
            terminal_outcome="delivered",
            completion_cert_enabled=True,
            certificate_built=True,
            certificate_rendered=False,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("completion_cert_enabled", 1),
        ("certificate_built", 1),
        ("certificate_rendered", 1),
    ],
)
def test_control_state_requires_real_booleans(field: str, value: object) -> None:
    kwargs = {
        "terminal_outcome": "delivered",
        "completion_cert_enabled": True,
        "certificate_built": True,
        "certificate_rendered": False,
    }
    kwargs[field] = value
    with pytest.raises(TypeError, match=field):
        build_completion_cert_participation(_identity(), **kwargs)


def test_unknown_terminal_outcome_fails_closed() -> None:
    with pytest.raises(ValueError, match="terminal_outcome"):
        build_completion_cert_participation(
            _identity(),
            terminal_outcome="prepared",
            completion_cert_enabled=True,
            certificate_built=True,
            certificate_rendered=False,
        )
