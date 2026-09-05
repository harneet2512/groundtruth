"""Terminal participation for the CompletionCertificate mediator.

The submit seam prepares refusals before the observation formatter runs.  Preparation
is not delivery: duplicate suppression, formatter failure, or an abandoned pending
result can still prevent the model from seeing any bytes.  This module therefore
accepts only the formatter's terminal outcome and produces participation solely for
an exact, final, delivered ``submit_refusal`` candidate.

It performs no I/O, reads no flags, commits no ledger rows, and makes no causality
claim.  The caller remains responsible for invoking it at the delivery commit point.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .control_participation import ControlParticipation, build_control_participation
from .evidence_envelope import content_signature, derive_dedup_key


COMPLETION_CERT_FEATURE_ID = "GT_COMPLETION_CERT"
COMPLETION_CERT_DECISION_SITE = "mini_seam.submit_gate.completion_certificate"
SUBMIT_REFUSAL_FACT_CLASS = "submit_refusal"

_TERMINAL_OUTCOMES = frozenset(
    {"delivered", "allow", "duplicate", "formatter_abort", "abandoned_pending"}
)


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def submit_refusal_candidate_id(final_candidate_text: str) -> str:
    """Return the canonical content-addressed ID for final refusal bytes.

    The five-component evidence identity is used directly, with the exact shipped
    refusal as its sole payload.  A renderer newline, fallback render, or any other
    byte change therefore produces a different candidate ID.
    """

    if not isinstance(final_candidate_text, str) or not final_candidate_text:
        raise ValueError("final_candidate_text must be non-empty text")
    signature = content_signature((final_candidate_text,), ())
    return derive_dedup_key(
        "submit_gate",
        SUBMIT_REFUSAL_FACT_CLASS,
        "",
        SUBMIT_REFUSAL_FACT_CLASS,
        signature,
    )


@dataclass(frozen=True)
class CompletionRefusalIdentity:
    """Identity observed only after exact formatter visibility is established."""

    final_candidate_text: str
    candidate_id: str
    iteration: int

    @property
    def candidate_sha256_16(self) -> str:
        return hashlib.sha256(
            self.final_candidate_text.encode("utf-8", "surrogatepass")
        ).hexdigest()[:16]


def _validate_delivered_identity(
    identity: object,
) -> CompletionRefusalIdentity:
    if not isinstance(identity, CompletionRefusalIdentity):
        raise TypeError("identity must be CompletionRefusalIdentity")
    if not isinstance(identity.final_candidate_text, str) or not identity.final_candidate_text:
        raise ValueError("final_candidate_text must be non-empty text")
    canonical_id = submit_refusal_candidate_id(identity.final_candidate_text)
    if not isinstance(identity.candidate_id, str) or identity.candidate_id != canonical_id:
        raise ValueError("candidate_id must match the canonical final refusal identity")
    if not _nonnegative_int(identity.iteration):
        raise ValueError("iteration must be a non-negative integer")
    return identity


def build_completion_cert_participation(
    identity: CompletionRefusalIdentity | None,
    *,
    terminal_outcome: str,
    completion_cert_enabled: bool,
    certificate_built: bool,
    certificate_rendered: bool,
) -> ControlParticipation | None:
    """Build the mediator decision for one terminal submit-refusal transaction.

    ``APPLIED`` means certificate fields shaped the final refusal bytes.
    ``NO_EFFECT`` means the certificate executed but preserved the Gate Kernel's head
    decision and the plain refusal was shipped.  Every non-delivery outcome, disabled
    control, or failed certificate build is intentionally silent.
    """

    for name, value in (
        ("completion_cert_enabled", completion_cert_enabled),
        ("certificate_built", certificate_built),
        ("certificate_rendered", certificate_rendered),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be a bool")
    if not isinstance(terminal_outcome, str) or terminal_outcome not in _TERMINAL_OUTCOMES:
        raise ValueError("terminal_outcome is not a recognized terminal state")
    if terminal_outcome != "delivered":
        return None
    if not completion_cert_enabled or not certificate_built:
        return None

    exact = _validate_delivered_identity(identity)
    return build_control_participation(
        feature_id=COMPLETION_CERT_FEATURE_ID,
        decision_site=COMPLETION_CERT_DECISION_SITE,
        decision="APPLIED" if certificate_rendered else "NO_EFFECT",
        iteration=exact.iteration,
        candidate_bytes=exact.final_candidate_text,
        fact_class=SUBMIT_REFUSAL_FACT_CLASS,
        candidate_id=exact.candidate_id,
        reason=(
            "completion_certificate_shaped_final_submit_refusal"
            if certificate_rendered
            else "completion_certificate_preserved_head_plain_submit_refusal"
        ),
    )


__all__ = [
    "COMPLETION_CERT_DECISION_SITE",
    "COMPLETION_CERT_FEATURE_ID",
    "SUBMIT_REFUSAL_FACT_CLASS",
    "CompletionRefusalIdentity",
    "build_completion_cert_participation",
    "submit_refusal_candidate_id",
]
