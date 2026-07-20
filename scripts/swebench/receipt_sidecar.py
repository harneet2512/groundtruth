"""Strict reader and trajectory-corroborated join for ``gt_receipt.v1``.

The runtime receipt sidecar is durable mechanism telemetry.  It can corroborate
that one sealed candidate advanced through the receipt ladder, but it is not a
substitute for model-authored trajectory evidence.  Accordingly, the join API
requires the caller to pass an independent trajectory-corroboration result before
it can report acknowledgment support.

All joins are fail-closed and use the complete delivery identity:

``(content_sha256_16, canonical candidate_id, observation_id, opportunity_id)``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from groundtruth.runtime.evidence_envelope import (
    EvidenceEnvelope,
    from_dict as envelope_from_dict,
    observation_binding_from_dict,
    observation_candidate_id,
    to_dict as envelope_to_dict,
    validate as validate_envelope,
    validate_observation_binding,
)
from groundtruth.runtime.feature_lineage import (
    DeliveryLineage,
    FeatureRef,
    build_lineage,
    lineage_to_dict,
)


RECEIPT_SCHEMA = "gt_receipt.v1"
COLLECTION_INTEGRITY_SCHEMA = "gt.receipt_collection_integrity.v1"
COLLECTION_FAILURE_GLOB = "gt_receipt_integrity_*.json"

_KINDS = frozenset({"lane", "gateway"})
_TRANSITION_RANK = {
    "delivered": 1,
    "referenced": 2,
    "acted": 3,
    "resolved_state": 4,
    "causal": 5,
}
_REQUIRED_FIELDS = frozenset(
    {"schema", "kind", "transition", "action_index", "ts_ms", "envelope"}
)
# SS-RCPT (2026-07-19): ``produced_dedup_key`` is the writer's PROOF that a
# law-(h) candidate mismatch is exactly a post-pool byte mutation (provenance
# line filter): the produced-text dedup key, computed by the same envelope
# builder. Tolerated ONLY when it equals binding.candidate_id — every other
# mismatch (e.g. a raw-key steer binding) stays fatal.
_OPTIONAL_FIELDS = frozenset({"lineage", "produced_dedup_key"})


@dataclass(frozen=True, order=True)
class ReceiptJoinKey:
    """Exact identity shared by one delivery and all of its receipt transitions."""

    content_sha256_16: str
    candidate_id: str
    observation_id: str
    opportunity_id: str


@dataclass(frozen=True)
class ReceiptIntegrityFailure:
    """One deterministic, machine-readable reason a sidecar cannot be trusted."""

    code: str
    line_number: int
    detail: str = ""


@dataclass(frozen=True)
class ReceiptRecord:
    """One validated ``gt_receipt.v1`` transition."""

    key: ReceiptJoinKey
    kind: str
    transition: str
    action_index: int
    ts_ms: int
    envelope: EvidenceEnvelope
    lineage: DeliveryLineage | None
    line_number: int


@dataclass(frozen=True)
class ReceiptSidecar:
    """Strict load result.  Any integrity failure makes the sidecar untrusted."""

    source: str
    source_present: bool
    sealed_delivery_expected: bool
    records: tuple[ReceiptRecord, ...]
    failures: tuple[ReceiptIntegrityFailure, ...]

    @property
    def integrity_ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class ReceiptJoinResult:
    """Fail-closed receipt/trajectory join result."""

    key: ReceiptJoinKey
    matched: bool
    integrity_ok: bool
    sidecar_transition: str | None
    trajectory_corroborated: bool
    acknowledgment_supported: bool
    failure_codes: tuple[str, ...]


def canonical_receipt_key(
    *,
    content_sha256_16: str,
    candidate_id: str,
    observation_id: str,
    opportunity_id: str,
) -> ReceiptJoinKey:
    """Build and validate the canonical receipt join key."""

    if not _is_lower_hex(content_sha256_16, 16):
        raise ValueError("content_sha256_16 must be exactly 16 lowercase hex characters")
    canonical_candidate = observation_candidate_id(candidate_id)
    if not _is_lower_hex(canonical_candidate, len(canonical_candidate)):
        raise ValueError("candidate_id did not canonicalize to lowercase hexadecimal")
    if not _is_lower_hex(observation_id, 64):
        raise ValueError("observation_id must be exactly 64 lowercase hex characters")
    if not _is_lower_hex(opportunity_id, 64):
        raise ValueError("opportunity_id must be exactly 64 lowercase hex characters")
    return ReceiptJoinKey(
        content_sha256_16=content_sha256_16,
        candidate_id=canonical_candidate,
        observation_id=observation_id,
        opportunity_id=opportunity_id,
    )


# CLASS-2(a): DECLARED per-layer evidence-authority exemptions. Some delivered rows are BOUND
# and SEALED (chars>0, valid observation_binding, consistent candidate_id) yet carry their
# consumption proof through a DIFFERENT durable authority than the EvidenceEnvelope receipt
# sidecar. This is a TYPED contract — each entry names WHERE that layer's proof actually lives —
# NOT a convenience narrowing to make integrity green: a layer is exempt only because its
# consumption evidence is provably persisted elsewhere. Any layer NOT listed here still MUST
# produce an envelope receipt when it delivers bound+sealed bytes.
_RECEIPT_EXEMPT_LAYERS: dict[str, str] = {
    # the sealed task-start brief ships as block-lineage receipts, never an EvidenceEnvelope
    # receipt (gt_agent._substrate_brief writes brief.txt directly; the seam does not seal it).
    "brief.task": "sealed brief blocks (block-lineage receipts)",
    # the submit-refusal completion gate persists its consumption proof as a PRODUCER ATTESTATION
    # (gt_mini_patch._persist_submit_refusal_producer_attestation, W2/B-ATT) re-running the pure
    # gate BLOCK on its own recorded inputs — it has NO envelope receipt path BY DESIGN, so a
    # bound+sealed submit_refusal delivered row must NOT demand a missing envelope receipt.
    "submit_refusal": "producer attestation (W2/B-ATT)",
}


def sealed_receipt_expected(runtime_rows: Iterable[object]) -> bool:
    """Whether ledger truth requires a durable EvidenceEnvelope receipt sidecar.

    Only physical deliveries owned by the envelope receipt path count. Layers in
    :data:`_RECEIPT_EXEMPT_LAYERS` are bound+sealed but prove consumption through a declared
    alternate authority (the task-start brief via block-lineage receipts; submit_refusal via the
    producer attestation), so they must not create a false missing-sidecar failure. Every other
    delivered, bound, sealed, identity-consistent row DOES require an envelope receipt.
    """

    for raw in runtime_rows:
        if not isinstance(raw, dict):
            continue
        if raw.get("layer") in _RECEIPT_EXEMPT_LAYERS or raw.get("outcome") != "delivered":
            continue
        try:
            chars = int(raw.get("chars_delivered") or 0)
        except (TypeError, ValueError):
            continue
        seal = raw.get("content_sha256_16")
        if chars <= 0 or not _is_lower_hex(seal, 16):
            continue
        try:
            binding = observation_binding_from_dict(raw.get("observation_binding"))
        except (TypeError, ValueError):
            continue
        if binding is None or validate_observation_binding(binding):
            continue
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        try:
            if observation_candidate_id(candidate_id) != binding.candidate_id:
                continue
        except ValueError:
            continue
        return True
    return False


def load_receipt_sidecar(
    path: str | Path,
    *,
    sealed_delivery_expected: bool,
    collection_integrity_paths: Iterable[str | Path] = (),
) -> ReceiptSidecar:
    """Load a receipt sidecar strictly, retaining every integrity failure.

    Missing receipt bytes are legitimate only when no sealed delivery was expected.
    A collection-failure marker always invalidates the sidecar, even if a partial file
    also exists.
    """

    source = Path(path)
    failures = list(_load_collection_failures(collection_integrity_paths))
    if not source.is_file():
        if sealed_delivery_expected:
            failures.append(
                ReceiptIntegrityFailure(
                    "receipt_sidecar_missing_for_sealed_delivery", 0, str(source)
                )
            )
        return ReceiptSidecar(
            source=str(source),
            source_present=False,
            sealed_delivery_expected=sealed_delivery_expected,
            records=(),
            failures=tuple(failures),
        )

    records: list[ReceiptRecord] = []
    try:
        raw_lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        failures.append(
            ReceiptIntegrityFailure("receipt_sidecar_unreadable", 0, type(exc).__name__)
        )
        return ReceiptSidecar(
            source=str(source),
            source_present=True,
            sealed_delivery_expected=sealed_delivery_expected,
            records=(),
            failures=tuple(failures),
        )

    if not raw_lines and sealed_delivery_expected:
        failures.append(
            ReceiptIntegrityFailure("receipt_sidecar_empty_for_sealed_delivery", 0)
        )

    for line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            failures.append(ReceiptIntegrityFailure("receipt_line_empty", line_number))
            continue
        try:
            raw = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            failures.append(
                ReceiptIntegrityFailure(
                    "receipt_line_invalid_json", line_number, type(exc).__name__
                )
            )
            continue
        try:
            records.append(_parse_record(raw, line_number))
        except ValueError as exc:
            code, _, detail = str(exc).partition(":")
            failures.append(
                ReceiptIntegrityFailure(code, line_number, detail)
            )

    failures.extend(_validate_transition_sequences(records))
    return ReceiptSidecar(
        source=str(source),
        source_present=True,
        sealed_delivery_expected=sealed_delivery_expected,
        records=tuple(records),
        failures=tuple(failures),
    )


def join_receipt_evidence(
    sidecar: ReceiptSidecar,
    key: ReceiptJoinKey,
    *,
    trajectory_corroborated: bool,
) -> ReceiptJoinResult:
    """Join one delivery to its receipt ladder.

    ``trajectory_corroborated`` must come from the registered, class-specific,
    model-authored trajectory predicate.  Sidecar state alone never supports
    acknowledgment.
    """

    failure_codes = tuple(failure.code for failure in sidecar.failures)
    if not sidecar.integrity_ok:
        return ReceiptJoinResult(
            key=key,
            matched=False,
            integrity_ok=False,
            sidecar_transition=None,
            trajectory_corroborated=bool(trajectory_corroborated),
            acknowledgment_supported=False,
            failure_codes=failure_codes,
        )

    matched = tuple(record for record in sidecar.records if record.key == key)
    if not matched:
        return ReceiptJoinResult(
            key=key,
            matched=False,
            integrity_ok=True,
            sidecar_transition=None,
            trajectory_corroborated=bool(trajectory_corroborated),
            acknowledgment_supported=False,
            failure_codes=("receipt_identity_not_found",),
        )

    transition = max(
        (record.transition for record in matched),
        key=_TRANSITION_RANK.__getitem__,
    )
    sidecar_ack = _TRANSITION_RANK[transition] >= _TRANSITION_RANK["referenced"]
    return ReceiptJoinResult(
        key=key,
        matched=True,
        integrity_ok=True,
        sidecar_transition=transition,
        trajectory_corroborated=bool(trajectory_corroborated),
        acknowledgment_supported=sidecar_ack and bool(trajectory_corroborated),
        failure_codes=(),
    )


def _parse_record(raw: object, line_number: int) -> ReceiptRecord:
    if not isinstance(raw, dict):
        raise ValueError("receipt_line_not_object")
    keys = frozenset(raw)
    if not _REQUIRED_FIELDS <= keys:
        raise ValueError("receipt_fields_missing")
    if keys - (_REQUIRED_FIELDS | _OPTIONAL_FIELDS):
        raise ValueError("receipt_fields_unknown")
    if raw.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("receipt_schema_invalid")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise ValueError("receipt_kind_invalid")
    transition = raw.get("transition")
    if transition not in _TRANSITION_RANK:
        raise ValueError("receipt_transition_invalid")
    action_index = _nonnegative_int(raw.get("action_index"), "receipt_action_index_invalid")
    ts_ms = _nonnegative_int(raw.get("ts_ms"), "receipt_timestamp_invalid")

    raw_envelope = raw.get("envelope")
    if not isinstance(raw_envelope, dict):
        raise ValueError("receipt_envelope_not_object")
    if tuple(raw_envelope) != EvidenceEnvelope.FIELD_ORDER:
        raise ValueError("receipt_envelope_fields_invalid")
    try:
        envelope = envelope_from_dict(raw_envelope)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"receipt_envelope_from_dict_invalid:{type(exc).__name__}"
        ) from exc
    if envelope_to_dict(envelope) != raw_envelope:
        raise ValueError("receipt_envelope_roundtrip_mismatch")
    binding = envelope.observation_binding
    # SS-RCPT: evidence-backed mutation proof — valid ONLY when the record carries the
    # produced-text dedup key AND it canonicalizes to the binding's candidate identity.
    produced_key = raw.get("produced_dedup_key")
    mutation_proven = bool(
        isinstance(produced_key, str)
        and produced_key
        and binding is not None
        and observation_candidate_id(produced_key) == binding.candidate_id
    )
    envelope_issues = validate_envelope(envelope)
    if mutation_proven:
        envelope_issues = [
            issue for issue in envelope_issues
            if issue != "observation_binding:candidate_id_mismatch"
        ]
    if envelope_issues:
        raise ValueError("receipt_envelope_invalid:" + ",".join(envelope_issues))
    if transition != envelope.receipt_state:
        raise ValueError("receipt_transition_state_mismatch")
    if not envelope.rendered_bytes_hash:
        raise ValueError("receipt_delivery_seal_missing")
    if binding is None:
        raise ValueError("receipt_observation_binding_missing")
    if (
        observation_candidate_id(envelope.dedup_key) != binding.candidate_id
        and not mutation_proven
    ):
        raise ValueError("receipt_candidate_identity_mismatch")

    lineage = _parse_lineage(raw.get("lineage")) if "lineage" in raw else None
    if lineage is not None:
        if lineage.runtime_producer_id != envelope.producer:
            raise ValueError("receipt_lineage_producer_mismatch")
        if lineage.evidence_type != envelope.evidence_type:
            raise ValueError("receipt_lineage_evidence_type_mismatch")

    key = canonical_receipt_key(
        content_sha256_16=envelope.rendered_bytes_hash[:16],
        candidate_id=binding.candidate_id,
        observation_id=binding.observation_id,
        opportunity_id=binding.opportunity_id,
    )
    return ReceiptRecord(
        key=key,
        kind=kind,
        transition=transition,
        action_index=action_index,
        ts_ms=ts_ms,
        envelope=envelope,
        lineage=lineage,
        line_number=line_number,
    )


def _parse_lineage(raw: object) -> DeliveryLineage:
    if not isinstance(raw, dict):
        raise ValueError("receipt_lineage_not_object")
    expected_fields = {
        "schema",
        "runtime_producer_id",
        "registered_producer_id",
        "producer_registration_match",
        "evidence_type",
        "fact_class",
        "features",
        "required_event",
        "actual_event",
        "receipt_predicate",
        "causal_eval",
        "causal_probe_id",
        "causal_contribution_proven",
        "reactive",
    }
    if set(raw) != expected_fields:
        raise ValueError("receipt_lineage_fields_invalid")
    features_raw = raw.get("features")
    if not isinstance(features_raw, list):
        raise ValueError("receipt_lineage_features_invalid")
    features: list[FeatureRef] = []
    try:
        for feature in features_raw:
            if not isinstance(feature, dict) or set(feature) != {
                "category", "feature_id", "role"
            }:
                raise ValueError("feature")
            features.append(
                FeatureRef(
                    category=feature["category"],
                    feature_id=feature["feature_id"],
                    role=feature["role"],
                )
            )
        lineage = DeliveryLineage(
            schema=raw["schema"],
            runtime_producer_id=raw["runtime_producer_id"],
            registered_producer_id=raw["registered_producer_id"],
            producer_registration_match=_strict_bool(
                raw["producer_registration_match"], "producer_registration_match"
            ),
            evidence_type=raw["evidence_type"],
            fact_class=raw["fact_class"],
            features=tuple(features),
            required_event=raw["required_event"],
            actual_event=raw["actual_event"],
            receipt_predicate=raw["receipt_predicate"],
            causal_eval=raw["causal_eval"],
            causal_probe_id=raw["causal_probe_id"],
            causal_contribution_proven=_strict_bool(
                raw["causal_contribution_proven"], "causal_contribution_proven"
            ),
            reactive=_strict_bool(raw["reactive"], "reactive"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"receipt_lineage_invalid:{type(exc).__name__}"
        ) from exc
    if lineage_to_dict(lineage) != raw:
        raise ValueError("receipt_lineage_roundtrip_mismatch")
    if not lineage.producer_registration_match:
        raise ValueError("receipt_lineage_unregistered_producer")
    cap_feature_ids = [
        feature.feature_id
        for feature in lineage.features
        if feature.category == "CAP"
    ]
    try:
        rebuilt = build_lineage(
            runtime_producer_id=lineage.runtime_producer_id,
            evidence_type=lineage.evidence_type,
            actual_event=lineage.actual_event,
            cap_feature_ids=cap_feature_ids,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"receipt_lineage_registry_rebuild_invalid:{type(exc).__name__}"
        ) from exc
    if rebuilt is None or lineage_to_dict(rebuilt) != raw:
        raise ValueError("receipt_lineage_registry_mismatch")
    return lineage


def _validate_transition_sequences(
    records: list[ReceiptRecord],
) -> tuple[ReceiptIntegrityFailure, ...]:
    failures: list[ReceiptIntegrityFailure] = []
    by_key: dict[ReceiptJoinKey, list[ReceiptRecord]] = {}
    for record in records:
        by_key.setdefault(record.key, []).append(record)
    for key in sorted(by_key):
        previous: ReceiptRecord | None = None
        for record in by_key[key]:
            if previous is None:
                if record.transition != "delivered":
                    failures.append(
                        ReceiptIntegrityFailure(
                            "receipt_sequence_missing_delivered",
                            record.line_number,
                        )
                    )
            else:
                if _TRANSITION_RANK[record.transition] <= _TRANSITION_RANK[
                    previous.transition
                ]:
                    failures.append(
                        ReceiptIntegrityFailure(
                            "receipt_transition_not_strictly_monotone",
                            record.line_number,
                        )
                    )
                if record.action_index < previous.action_index:
                    failures.append(
                        ReceiptIntegrityFailure(
                            "receipt_action_index_regressed",
                            record.line_number,
                        )
                    )
                if record.kind != previous.kind:
                    failures.append(
                        ReceiptIntegrityFailure(
                            "receipt_kind_changed",
                            record.line_number,
                        )
                    )
                if not _same_delivery(previous.envelope, record.envelope):
                    failures.append(
                        ReceiptIntegrityFailure(
                            "receipt_delivery_identity_changed",
                            record.line_number,
                        )
                    )
                if previous.lineage != record.lineage:
                    failures.append(
                        ReceiptIntegrityFailure(
                            "receipt_lineage_changed",
                            record.line_number,
                        )
                    )
            previous = record
    return tuple(failures)


def _same_delivery(left: EvidenceEnvelope, right: EvidenceEnvelope) -> bool:
    left_dict = envelope_to_dict(left)
    right_dict = envelope_to_dict(right)
    left_dict["receipt_state"] = ""
    right_dict["receipt_state"] = ""
    return left_dict == right_dict


def _load_collection_failures(
    paths: Iterable[str | Path],
) -> tuple[ReceiptIntegrityFailure, ...]:
    failures: list[ReceiptIntegrityFailure] = []
    for path_value in paths:
        path = Path(path_value)
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            failures.append(
                ReceiptIntegrityFailure(
                    "receipt_collection_marker_invalid", 0, type(exc).__name__
                )
            )
            continue
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != COLLECTION_INTEGRITY_SCHEMA
            or raw.get("status") != "FAIL"
            or not isinstance(raw.get("reason"), str)
            or not raw["reason"]
        ):
            failures.append(
                ReceiptIntegrityFailure("receipt_collection_marker_invalid", 0)
            )
            continue
        failures.append(
            ReceiptIntegrityFailure(str(raw["reason"]), 0, str(path))
        )
    return tuple(failures)


def _strict_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be bool")
    return value


def _nonnegative_int(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(code)
    return value


def _is_lower_hex(value: object, width: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "COLLECTION_FAILURE_GLOB",
    "COLLECTION_INTEGRITY_SCHEMA",
    "RECEIPT_SCHEMA",
    "ReceiptIntegrityFailure",
    "ReceiptJoinKey",
    "ReceiptJoinResult",
    "ReceiptRecord",
    "ReceiptSidecar",
    "canonical_receipt_key",
    "join_receipt_evidence",
    "load_receipt_sidecar",
    "sealed_receipt_expected",
]
