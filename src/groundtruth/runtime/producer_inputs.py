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
class CallerUsageEvidenceRow:
    """One source-attributed ``caller_usage`` property for a FACT caller.

    The producer retains only usage rows whose graph property identifies the
    exact caller node, callee, source line, extractor confidence, and source
    revision.  Missing fields stay missing upstream; callers must never infer a
    bilateral contract from a free-form property value alone.
    """

    property_id: int
    caller_node_id: int
    caller_identity: str
    caller_file: str
    usage_kind: str
    callee: str
    call_site: str
    line: int
    confidence: float
    source_revision: str
    extractor: str
    evidence_method: str


@dataclass(frozen=True, order=True)
class DefinitionRow:
    """One typed definition-site row retained from a post_search def-partition read.

    The gateway def-partition producers (``_produce_def_ref_partition`` /
    ``_produce_name_fold`` / ``_produce_wrong_surface`` / ``_produce_body``) resolve a
    searched symbol to concrete graph DEFINITION nodes at a fixed graph revision. This
    record preserves exactly what the producer consumed to render the partition: the
    node's ``file``/``line``/``kind`` (label) and ``definition_id`` (graph node id).

    ``confidence`` / ``resolution_method`` are OPTIONAL: a bare definition node has no
    edge-style resolution provenance (it is an exact-name node lookup), so a body-concept
    hit or an exact def-node lookup leaves them ``None`` rather than inferring a value.
    """

    identity: str  # the matched node name (the searched symbol / fold variant)
    file: str
    line: int
    kind: str  # the graph node label (Function / Method / Class / ...)
    definition_id: int
    confidence: float | None = None
    resolution_method: str | None = None


@dataclass(frozen=True, order=True)
class RepositoryWitnessRow:
    """One exact source location retained for a repository-derived claim.

    ``kind`` identifies the deterministic substrate, while ``source_state``
    binds the path and line to the exact file bytes observed by the producer.
    The row is render-neutral and makes no stronger claim than that source
    location.
    """

    file: str
    line: int
    kind: str
    identity: str
    source_state: SourceState | None


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
    caller_usage_rows: tuple[CallerUsageEvidenceRow, ...] = ()
    # def_partition (post_search) structured search evidence. Empty for every edit-fact
    # producer (caller_break / signature_mismatch), so an existing producer's canonical
    # input bytes are byte-identical. ``query_identity`` is the exact search operand the
    # producer resolved (the symbol whose definitions were partitioned).
    definition_rows: tuple[DefinitionRow, ...] = ()
    query_identity: str = ""
    # Exact repository witnesses retained without changing the legacy envelope
    # provenance/render/dedup identity.  The canonical converter may consume
    # these only after validating the row and its source-state binding.
    repository_witness_rows: tuple[RepositoryWitnessRow, ...] = ()


__all__ = [
    "PRODUCER_INPUTS_SCHEMA",
    "CallerEvidenceRow",
    "CallerUsageEvidenceRow",
    "DefinitionRow",
    "ProducerInputs",
    "RepositoryWitnessRow",
    "SignatureChange",
    "SourceState",
]
