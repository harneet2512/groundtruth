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


__all__ = [
    "PRODUCER_INPUTS_SCHEMA",
    "CallerEvidenceRow",
    "ProducerInputs",
    "SourceState",
]
