"""Pure factory for producer-owned Gateway truth/freshness attestations.

The factory consumes a *final delivered* envelope and its render-neutral
``ProducerInputs`` sidecar. It performs no I/O and does not promote SS-LIVE. Only
the two Gateway producer pairs whose semantic inputs are currently complete are
supported: caller_break/caller_contract and
signature_mismatch/signature_delta. Source-state artifacts are canonical
producer-observation descriptors (file/hash/revision), not source-file bytes;
the exact final model-visible bytes are stored separately as rendered_candidate.
"""

from __future__ import annotations

import hashlib
import json
from typing import AbstractSet, Any

from .control_participation import ControlParticipation, build_control_participation
from .evidence_envelope import EvidenceEnvelope
from .fact_registry import EVENTS, producer_matches, registration_for, required_event
from .producer_attestation import (
    ATTESTATION_SCHEMA,
    FRESHNESS,
    PASS,
    TRUTH,
    UNMEASURED,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
    validate,
)
from .producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    CallerUsageEvidenceRow,
    DefinitionRow,
    ProducerInputs,
    SignatureChange,
    SourceState,
)


# Every gateway evidence_type whose semantic producer inputs are complete enough to
# attest. The def-partition family (all four post_search variants) shares the canonical
# ``def_partition`` fact class; each is bound to its own DefinitionRow evidence.
_SUPPORTED: dict[str, str] = {
    "caller_break": "caller_contract",
    "signature_mismatch": "signature_delta",
    "def_ref_partition": "def_partition",
    "name_fold": "def_partition",
    "wrong_surface": "def_partition",
    "body_concept": "def_partition",
}

# The def-partition family: search-based facts (no before/after edit state, no caller
# rows, no signature change) — their truth basis is the typed DefinitionRow set + the
# graph revision they were read at + the exact query identity.
_DEF_PARTITION_EVIDENCE: frozenset[str] = frozenset(
    {
        "def_ref_partition",
        "name_fold",
        "wrong_surface",
        "body_concept",
    }
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _source_dict(state: SourceState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "file": state.file,
        "sha256": state.sha256,
        "revision": state.revision,
    }


def _caller_dict(row: CallerEvidenceRow) -> dict[str, Any]:
    return {
        "identity": row.identity,
        "file": row.file,
        "line": row.line,
        "confidence": row.confidence,
        "resolution_method": row.resolution_method,
        "edge_id": row.edge_id,
        "definition_id": row.definition_id,
        "source_state": _source_dict(row.source_state),
    }


def _change_dict(change: SignatureChange) -> dict[str, Any]:
    return {
        "symbol": change.symbol,
        "edited_file": change.edited_file,
        "before_parameters": (
            list(change.before_parameters) if change.before_parameters is not None else None
        ),
        "after_parameters": (
            list(change.after_parameters) if change.after_parameters is not None else None
        ),
        "old_min_params": change.old_min_params,
        "old_max_params": change.old_max_params,
        "new_min_params": change.new_min_params,
        "new_max_params": change.new_max_params,
        "positional_args": change.positional_args,
    }


def _definition_dict(row: DefinitionRow) -> dict[str, Any]:
    return {
        "identity": row.identity,
        "file": row.file,
        "line": row.line,
        "kind": row.kind,
        "definition_id": row.definition_id,
        "confidence": row.confidence,
        "resolution_method": row.resolution_method,
    }


def _usage_dict(row: CallerUsageEvidenceRow) -> dict[str, Any]:
    return {
        "property_id": row.property_id,
        "caller_node_id": row.caller_node_id,
        "caller_identity": row.caller_identity,
        "caller_file": row.caller_file,
        "usage_kind": row.usage_kind,
        "callee": row.callee,
        "call_site": row.call_site,
        "line": row.line,
        "confidence": row.confidence,
        "source_revision": row.source_revision,
        "extractor": row.extractor,
        "evidence_method": row.evidence_method,
    }


def _input_payload(
    envelope: EvidenceEnvelope,
    *,
    delivery_seal: str,
    actual_event: str,
    open_event: str,
) -> dict[str, Any]:
    inputs = envelope.producer_inputs
    if not isinstance(inputs, ProducerInputs):
        raise ValueError("producer inputs missing or wrong type")
    registration = registration_for(envelope.evidence_type)
    if registration is None:
        raise ValueError("unsupported unregistered evidence type")
    payload: dict[str, Any] = {
        "schema": "gt.gateway_attestation_inputs.v1",
        "producer_inputs_schema": inputs.schema,
        "evidence_type": envelope.evidence_type,
        "fact_class": registration.fact_class,
        "candidate_id": inputs.candidate_id,
        "delivery_seal": delivery_seal,
        "runtime_producer_id": envelope.producer,
        "registered_producer_id": registration.producer,
        "actual_event": actual_event,
        "open_event": open_event,
        "required_event": required_event(envelope.evidence_type),
        "before_state": _source_dict(inputs.before_state),
        "after_state": _source_dict(inputs.after_state),
        "caller_rows": [_caller_dict(row) for row in inputs.caller_rows],
        "graph_revision": inputs.graph_revision,
        "signature_changes": [_change_dict(change) for change in inputs.signature_changes],
        "caller_usage_rows": [_usage_dict(row) for row in inputs.caller_usage_rows],
    }
    # def-partition structured search evidence — additive keys emitted ONLY when the
    # producer carried them, so every edit-fact (caller_break / signature_mismatch)
    # canonical byte payload is byte-identical to the pre-def-partition era.
    if inputs.definition_rows or inputs.query_identity:
        payload["query_identity"] = inputs.query_identity
        payload["definition_rows"] = [_definition_dict(row) for row in inputs.definition_rows]
    return payload


def canonical_producer_inputs_bytes(
    envelope: EvidenceEnvelope,
    *,
    delivery_seal: str,
    actual_event: str,
    open_event: str,
) -> bytes:
    """Canonical bytes binding every producer input and delivery/event identity."""

    return _canonical_json(
        _input_payload(
            envelope,
            delivery_seal=delivery_seal,
            actual_event=actual_event,
            open_event=open_event,
        )
    )


def _valid_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _complete_source(state: SourceState | None) -> bool:
    return bool(
        isinstance(state, SourceState)
        and state.file.strip()
        and _valid_sha(state.sha256)
        and state.revision.strip()
    )


def _complete_caller(row: CallerEvidenceRow) -> bool:
    return bool(
        row.identity.strip()
        and row.file.strip()
        and isinstance(row.line, int)
        and not isinstance(row.line, bool)
        and row.line > 0
        and isinstance(row.confidence, (int, float))
        and not isinstance(row.confidence, bool)
        and 0.7 <= float(row.confidence) <= 1.0
        and isinstance(row.resolution_method, str)
        and row.resolution_method.strip()
        and isinstance(row.edge_id, int)
        and not isinstance(row.edge_id, bool)
        and row.edge_id > 0
        and isinstance(row.definition_id, int)
        and not isinstance(row.definition_id, bool)
        and row.definition_id > 0
        and _complete_source(row.source_state)
    )


def _caller_change_complete(change: SignatureChange) -> bool:
    return bool(
        change.symbol.strip()
        and change.edited_file.strip()
        and isinstance(change.before_parameters, tuple)
        and isinstance(change.after_parameters, tuple)
        and change.before_parameters != change.after_parameters
        and all(isinstance(item, str) and item for item in change.before_parameters)
        and all(isinstance(item, str) and item for item in change.after_parameters)
        and all(
            value is None
            for value in (
                change.old_min_params,
                change.old_max_params,
                change.new_min_params,
                change.new_max_params,
                change.positional_args,
            )
        )
    )


def _signature_change_complete(change: SignatureChange) -> bool:
    values = (
        change.old_min_params,
        change.old_max_params,
        change.new_min_params,
        change.new_max_params,
        change.positional_args,
    )
    if not (
        change.symbol.strip()
        and change.edited_file.strip()
        and change.before_parameters is None
        and change.after_parameters is None
        and all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    ):
        return False
    old_min, old_max, new_min, new_max, positional = values
    return bool(
        0 <= old_min <= old_max
        and 0 <= new_min <= new_max
        and positional >= 0
        and old_min <= positional <= old_max
        and not (new_min <= positional <= new_max)
    )


def _complete_usage(row: CallerUsageEvidenceRow) -> bool:
    return bool(
        isinstance(row.property_id, int)
        and not isinstance(row.property_id, bool)
        and row.property_id > 0
        and isinstance(row.caller_node_id, int)
        and not isinstance(row.caller_node_id, bool)
        and row.caller_node_id > 0
        and row.caller_identity.strip()
        and row.caller_file.strip()
        and row.usage_kind
        in {
            "boolean_check",
            "destructure_tuple",
            "exception_guard",
            "iterated",
        }
        and row.callee.strip()
        and row.call_site.strip()
        and isinstance(row.line, int)
        and not isinstance(row.line, bool)
        and row.line > 0
        and isinstance(row.confidence, (int, float))
        and not isinstance(row.confidence, bool)
        and 0.7 <= float(row.confidence) <= 1.0
        and row.source_revision.strip()
        and row.extractor.strip()
        and row.evidence_method.strip()
    )


def _complete_definition(row: DefinitionRow) -> bool:
    """A def-partition definition row is complete when it names a concrete graph node.

    Confidence / resolution_method are OPTIONAL (a bare def-node lookup has neither); when
    present they must be well-formed. Identity, file, a positive line, a non-empty kind,
    and a positive node id are all required — a fabricated row with a zero/absent id or a
    bad line can never be complete (the ``def-partition row not consumed by producer``
    mutation bites here)."""
    if not (
        isinstance(row.identity, str)
        and row.identity.strip()
        and isinstance(row.file, str)
        and row.file.strip()
        and isinstance(row.line, int)
        and not isinstance(row.line, bool)
        and row.line > 0
        and isinstance(row.kind, str)
        and row.kind.strip()
        and isinstance(row.definition_id, int)
        and not isinstance(row.definition_id, bool)
        and row.definition_id > 0
    ):
        return False
    if row.confidence is not None and not (
        isinstance(row.confidence, (int, float))
        and not isinstance(row.confidence, bool)
        and 0.0 <= float(row.confidence) <= 1.0
    ):
        return False
    if row.resolution_method is not None and not (
        isinstance(row.resolution_method, str) and row.resolution_method.strip()
    ):
        return False
    return True


def _artifact_bundle(
    envelope: EvidenceEnvelope,
    payload_bytes: bytes,
    shipped_bytes: bytes,
) -> tuple[tuple[ArtifactRef, ...], dict[str, bytes]]:
    inputs = envelope.producer_inputs
    assert isinstance(inputs, ProducerInputs)
    candidate = inputs.candidate_id
    artifacts: dict[str, bytes] = {}
    refs: list[ArtifactRef] = []

    def add(artifact_id: str, kind: str, revision: str, raw: bytes) -> None:
        artifacts[artifact_id] = raw
        refs.append(
            ArtifactRef(
                kind=kind,
                artifact_id=artifact_id,
                sha256=hashlib.sha256(raw).hexdigest(),
                revision=revision,
            )
        )

    identity = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    add(
        f"producer-inputs-{identity}.json",
        "producer_inputs",
        f"candidate:{candidate}",
        payload_bytes,
    )
    add(
        f"rendered-candidate-{identity}.bin",
        "rendered_candidate",
        f"delivery:{hashlib.sha256(shipped_bytes).hexdigest()[:16]}",
        shipped_bytes,
    )
    states: list[tuple[str, SourceState | None]] = [
        ("before", inputs.before_state),
        ("after", inputs.after_state),
    ]
    states.extend(
        (f"caller:{index}", row.source_state) for index, row in enumerate(inputs.caller_rows)
    )
    for index, (role, state) in enumerate(states):
        if not _complete_source(state):
            continue
        assert state is not None
        raw = _canonical_json({"role": role, **_source_dict(state)})
        add(f"source-state-{identity}-{index}.json", "source_state", state.revision, raw)
    return tuple(sorted(refs)), artifacts


def _predicate(
    *,
    kind: str,
    predicate_id: str,
    subject: str,
    expectation: str,
    complete: bool,
    producer_input_ref: ArtifactRef,
    rendered_ref: ArtifactRef,
    rendered_length: int,
    field_paths: tuple[str, ...],
) -> PredicateAttestation:
    proof_refs = (
        tuple(
            sorted(
                (
                    *(
                        ProofRef(
                            proof_type="producer_input",
                            artifact=producer_input_ref,
                            field_path=field_path,
                        )
                        for field_path in field_paths
                    ),
                    ProofRef(
                        proof_type="rendered_candidate",
                        artifact=rendered_ref,
                        field_path=f"bytes[0:{rendered_length}]",
                    ),
                )
            )
        )
        if complete
        else ()
    )
    return PredicateAttestation(
        predicate_kind=kind,
        predicate_id=predicate_id,
        subject=subject,
        expectation=expectation,
        observation="complete producer-owned structured inputs" if complete else "",
        verdict=PASS if complete else UNMEASURED,
        proof_refs=proof_refs,
    )


def build_gateway_attestation(
    envelope: EvidenceEnvelope,
    *,
    delivery_seal: str,
    shipped_bytes: bytes,
    actual_event: str,
    open_event: str,
) -> tuple[ProducerAttestation, dict[str, bytes]]:
    """Build one validated producer attestation plus exact artifact bytes."""

    expected_fact_class = _SUPPORTED.get(envelope.evidence_type)
    if expected_fact_class is None:
        raise ValueError(f"unsupported Gateway evidence type: {envelope.evidence_type!r}")
    inputs = envelope.producer_inputs
    if not isinstance(inputs, ProducerInputs):
        raise ValueError("producer inputs missing or wrong type")
    registration = registration_for(envelope.evidence_type)
    if registration is None or registration.fact_class != expected_fact_class:
        raise ValueError("unsupported registry fact-class binding")
    if not producer_matches(envelope.evidence_type, envelope.producer):
        raise ValueError("producer is not authoritative for evidence type")
    if inputs.schema != PRODUCER_INPUTS_SCHEMA:
        raise ValueError("producer inputs schema mismatch")
    if inputs.evidence_type != envelope.evidence_type:
        raise ValueError("producer inputs evidence type mismatch")
    if inputs.candidate_id != envelope.dedup_key:
        raise ValueError("producer inputs candidate mismatch")
    if not (
        isinstance(delivery_seal, str)
        and len(delivery_seal) == 16
        and all(char in "0123456789abcdef" for char in delivery_seal)
    ):
        raise ValueError("delivery seal must be exactly 16 lowercase hex")
    if not isinstance(shipped_bytes, bytes) or not shipped_bytes:
        raise ValueError("shipped bytes must be nonempty exact bytes")
    if hashlib.sha256(shipped_bytes).hexdigest()[:16] != delivery_seal:
        raise ValueError("delivery seal does not match shipped bytes")
    if actual_event not in EVENTS or open_event not in EVENTS:
        raise ValueError("actual/open event must be a registered fine event")

    raw = canonical_producer_inputs_bytes(
        envelope,
        delivery_seal=delivery_seal,
        actual_event=actual_event,
        open_event=open_event,
    )
    source_refs, artifacts = _artifact_bundle(envelope, raw, shipped_bytes)
    producer_input_ref = next(ref for ref in source_refs if ref.kind == "producer_inputs")
    rendered_ref = next(ref for ref in source_refs if ref.kind == "rendered_candidate")
    if envelope.evidence_type in _DEF_PARTITION_EVIDENCE:
        # def-partition (post_search) FACT: the truth basis is the typed DefinitionRow
        # set read at the graph revision the envelope shipped; no edit before/after state
        # and no signature change exist for a search fact. Fail closed if the producer
        # carried no definition rows, an incomplete row, no query identity, or a graph
        # revision that does not match the delivered envelope's.
        graph_bound = bool(
            inputs.graph_revision and inputs.graph_revision == envelope.graph_revision
        )
        rows_complete = bool(
            inputs.definition_rows
            and all(_complete_definition(row) for row in inputs.definition_rows)
        )
        truth_complete = bool(graph_bound and rows_complete and inputs.query_identity.strip())
        # Freshness for def_partition is the graph revision the definitions were read at
        # (registry freshness_deps = nodes + edges_rev); the delivered seal proves what
        # shipped, the graph revision binds it to the exact graph snapshot.
        freshness_complete = truth_complete
        truth_paths = ("$.definition_rows", "$.query_identity")
        freshness_paths = ("$.graph_revision",)
    else:
        common_complete = bool(
            _complete_source(inputs.before_state)
            and _complete_source(inputs.after_state)
            and inputs.caller_rows
            and all(_complete_caller(row) for row in inputs.caller_rows)
            and inputs.graph_revision
            and inputs.graph_revision == envelope.graph_revision
        )
        usage_complete = bool(
            not inputs.caller_usage_rows
            or (
                all(_complete_usage(row) for row in inputs.caller_usage_rows)
                and all(
                    row.source_revision == inputs.graph_revision for row in inputs.caller_usage_rows
                )
                and all(
                    any(
                        caller.identity == usage.caller_identity
                        and caller.file == usage.caller_file
                        for caller in inputs.caller_rows
                    )
                    for usage in inputs.caller_usage_rows
                )
            )
        )
        if envelope.evidence_type == "caller_break":
            semantic_complete = bool(
                len(inputs.signature_changes) == 1
                and _caller_change_complete(inputs.signature_changes[0])
            )
        else:
            semantic_complete = bool(
                len(inputs.signature_changes) == 1
                and _signature_change_complete(inputs.signature_changes[0])
            )
        truth_complete = common_complete and semantic_complete and usage_complete
        freshness_complete = common_complete and semantic_complete and usage_complete
        truth_paths = (
            "$.caller_rows",
            "$.signature_changes",
            *(("$.caller_usage_rows",) if inputs.caller_usage_rows else ()),
        )
        freshness_paths = (
            "$.before_state.sha256",
            "$.before_state.revision",
            "$.after_state.sha256",
            "$.after_state.revision",
            "$.graph_revision",
            *tuple(
                path
                for index in range(len(inputs.caller_rows))
                for path in (
                    f"$.caller_rows[{index}].source_state.sha256",
                    f"$.caller_rows[{index}].source_state.revision",
                )
            ),
            *tuple(
                f"$.caller_usage_rows[{index}].source_revision"
                for index in range(len(inputs.caller_usage_rows))
            ),
        )

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type=envelope.evidence_type,
        runtime_producer_id=envelope.producer,
        registered_producer_id=registration.producer,
        candidate_id=envelope.dedup_key,
        delivery_seal=delivery_seal,
        source_artifacts=source_refs,
        truth_predicates=(
            _predicate(
                kind=TRUTH,
                predicate_id=f"{envelope.evidence_type}.semantic_truth",
                subject=envelope.dedup_key,
                expectation="structured producer inputs prove the emitted semantic change",
                complete=truth_complete,
                producer_input_ref=producer_input_ref,
                rendered_ref=rendered_ref,
                rendered_length=len(shipped_bytes),
                field_paths=truth_paths,
            ),
        ),
        freshness_predicates=(
            _predicate(
                kind=FRESHNESS,
                predicate_id=f"{envelope.evidence_type}.candidate_freshness",
                subject=envelope.dedup_key,
                expectation="source and graph revisions bind the final candidate",
                complete=freshness_complete,
                producer_input_ref=producer_input_ref,
                rendered_ref=rendered_ref,
                rendered_length=len(shipped_bytes),
                field_paths=freshness_paths,
            ),
        ),
        decision=DecisionBinding(
            decision_key=registration.target_decision,
            open_event=open_event,
            required_event=required_event(envelope.evidence_type) or "",
        ),
    )
    errors = validate(attestation)
    if errors:
        raise ValueError("invalid producer attestation: " + ";".join(errors))
    return attestation, artifacts


_CALLER_CONTRACT_CONTROL_SITES = {
    "GT_CONTRACT_NATIVE": "gateway.caller_contract.native_render",
    "GT_CONTRACT_MODE": "gateway.caller_contract.mode_selection",
    "GT_CONTRACT_BILATERAL": "gateway.caller_contract.bilateral_selection",
    "GT_EVIDENCE_NATIVE": "gateway.caller_contract.caller_rows",
}


def build_caller_contract_control_participation(
    envelope: EvidenceEnvelope,
    *,
    attestation: ProducerAttestation,
    shipped_bytes: bytes,
    native: bool,
    enabled_features: AbstractSet[str],
    iteration: int,
) -> tuple[ControlParticipation, ...]:
    """Bind caller-contract CAP participation to one exact attested dose.

    This pure integration API must be called only after the Gateway winner has
    sealed.  It emits no delivery and never mentions ``GT_GATEWAY_NATIVE``: the
    generic native adapter cannot claim caller-specific mode, caller-row, or
    bilateral selection.  Any authority/seal/render mismatch is quiet.
    """
    if (
        not native
        or envelope.evidence_type != "caller_break"
        or envelope.producer != "caller_contract"
        or not isinstance(attestation, ProducerAttestation)
        or validate(attestation)
        or attestation.truth_verdict != PASS
        or attestation.freshness_verdict != PASS
        or attestation.candidate_id != envelope.dedup_key
        or not isinstance(shipped_bytes, bytes)
        or not shipped_bytes
        or hashlib.sha256(shipped_bytes).hexdigest()[:16] != attestation.delivery_seal
        or not isinstance(iteration, int)
        or isinstance(iteration, bool)
        or iteration < 0
    ):
        return ()
    try:
        from .adapters.miniswe import render_envelope

        expected = render_envelope(envelope, native=True).encode("utf-8", "surrogatepass")
        input_bytes = canonical_producer_inputs_bytes(
            envelope,
            delivery_seal=attestation.delivery_seal,
            # `open_event`, NOT `required_event`. The bytes were built with the OBSERVED
            # event, and `DecisionBinding` never persists `actual_event` -- but the sole
            # production call site builds the attestation with
            # ``actual_event=X, open_event=X``, so `open_event` IS the observed event and this
            # reproduces the original bytes exactly.
            #
            # Substituting the CONTRACTED event matched only when the delivery happened to be
            # on time. Off-boundary deliveries hash-mismatched and their participation record
            # was dropped silently, so a CAP participation record could only ever exist for an
            # ON-TIME delivery -- every statistic over them was conditioned on the very
            # property it was meant to measure.
            actual_event=attestation.decision.open_event,
            open_event=attestation.decision.open_event,
        )
    except Exception:
        return ()
    if shipped_bytes not in (expected, b"\n" + expected):
        return ()
    producer_input_refs = tuple(
        ref for ref in attestation.source_artifacts if ref.kind == "producer_inputs"
    )
    rendered_refs = tuple(
        ref for ref in attestation.source_artifacts if ref.kind == "rendered_candidate"
    )
    if (
        len(producer_input_refs) != 1
        or producer_input_refs[0].sha256 != hashlib.sha256(input_bytes).hexdigest()
        or len(rendered_refs) != 1
        or rendered_refs[0].sha256 != hashlib.sha256(shipped_bytes).hexdigest()
    ):
        return ()
    inputs = envelope.producer_inputs
    if not isinstance(inputs, ProducerInputs):
        return ()
    selected = set(enabled_features) & set(_CALLER_CONTRACT_CONTROL_SITES)
    rows: list[ControlParticipation] = []
    candidate_text = shipped_bytes.decode("utf-8", "surrogatepass")
    for feature_id in sorted(selected):
        decision = "APPLIED"
        reason = {
            "GT_CONTRACT_NATIVE": "canonical_native_contract_committed",
            "GT_CONTRACT_MODE": "signature_change_mode_selected",
            "GT_EVIDENCE_NATIVE": "typed_caller_rows_rendered",
        }.get(feature_id, "typed_caller_usage_rendered")
        if feature_id == "GT_CONTRACT_BILATERAL" and not inputs.caller_usage_rows:
            decision = "NO_EFFECT"
            reason = "no_typed_caller_usage"
        rows.append(
            build_control_participation(
                feature_id=feature_id,
                decision_site=_CALLER_CONTRACT_CONTROL_SITES[feature_id],
                decision=decision,
                iteration=iteration,
                candidate_bytes=candidate_text,
                fact_class="caller_contract",
                candidate_id=envelope.dedup_key,
                reason=reason,
            )
        )
    return tuple(rows)


__all__ = [
    "build_caller_contract_control_participation",
    "build_gateway_attestation",
    "canonical_producer_inputs_bytes",
]
