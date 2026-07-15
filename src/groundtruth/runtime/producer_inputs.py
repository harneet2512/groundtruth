"""Immutable, render-neutral inputs retained by evidence producers.

These records preserve the structured source facts needed to construct a later
producer attestation.  They are not attestations and make no truth claim.  The
records never render, serialize through ``EvidenceEnvelope``, or participate in
evidence/dedup identity.
"""

from __future__ import annotations

from dataclasses import dataclass


PRODUCER_INPUTS_SCHEMA = "gt.producer_inputs.v1"


@dataclass(frozen=True, order=True)
class SourceState:
    """Exact source content identity at one producer-observed revision."""

    file: str
    sha256: str
    revision: str


@dataclass(frozen=True, order=True)
class CallerEvidenceRow:
    """One typed caller row retained from a FACT-tier graph result."""

    identity: str
    file: str
    line: int
    confidence: float | None
    resolution_method: str | None
    source_state: SourceState | None
    edge_id: int | None = None
    definition_id: int | None = None


@dataclass(frozen=True, order=True)
class SignatureChange:
    """Exact semantic signature delta used by a producer decision.

    Cross-language caller-contract detection records parameter identities. The
    Python arity checker records positional bounds and the observed call arity.
    Inapplicable fields are ``None`` rather than inferred.
    """

    symbol: str
    edited_file: str
    before_parameters: tuple[str, ...] | None
    after_parameters: tuple[str, ...] | None
    old_min_params: int | None
    old_max_params: int | None
    new_min_params: int | None
    new_max_params: int | None
    positional_args: int | None


@dataclass(frozen=True)
class ProducerInputs:
    """Structured inputs for one final evidence candidate."""

    schema: str
    evidence_type: str
    candidate_id: str
    before_state: SourceState | None
    after_state: SourceState | None
    caller_rows: tuple[CallerEvidenceRow, ...]
    graph_revision: str
    signature_changes: tuple[SignatureChange, ...] = ()


__all__ = [
    "PRODUCER_INPUTS_SCHEMA",
    "CallerEvidenceRow",
    "ProducerInputs",
    "SignatureChange",
    "SourceState",
]
