"""Read-only, secret-free proof projection of the canonical runtime journal.

This module is deliberately outside the runtime's state-owning path.  It opens
the SQLite journal read-only, revalidates the append-only delivery,
compilation, and evidence projections, and emits only audit metadata.  Raw
observations, capsule text, provider payloads, response bodies, credentials,
and provider response identifiers never leave the reader.

``DELIVERED`` means a complete exact-payload provider-terminal chain.  It does
not mean that the model acknowledged, followed, or was behaviorally influenced
by the evidence; those remain separate, unmeasured trajectory questions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from groundtruth.runtime.feature_lineage import CAP_BYTE_OWNER_MECHANISMS
from groundtruth.runtime.reasoning_runtime import (
    DECISION_CAPSULE_SCHEMA as _DECISION_CAPSULE_SCHEMA,
    EvidenceLifecycle,
    EvidenceTransitionReason,
    _EVIDENCE_REASON_TRANSITIONS,
    projected_decision_window,
)


SCHEMA = "gt.runtime_attestation.v1"
BUNDLE_SCHEMA = "gt.runtime_attestation_bundle.v1"
_REQUIRED_TABLES = frozenset(
    {
        "delivery_attempts",
        "delivery_journal",
        "compilation_journal",
        "evidence_attempt_journal",
    }
)
_SUCCESS_PREFIX = (
    "SELECTED",
    "COMPILED",
    "JOINED",
    "DISPATCHED",
    "PROVIDER_ACCEPTED",
    "DELIVERED",
)
_ALLOWED_NEXT = {
    "SELECTED": frozenset({"COMPILED"}),
    "COMPILED": frozenset({"JOINED", "JOIN_FAILED"}),
    "JOINED": frozenset({"DISPATCHED"}),
    "DISPATCHED": frozenset(
        {"PROVIDER_ACCEPTED", "DISPATCH_FAILED", "PROVIDER_REJECTED"}
    ),
    "PROVIDER_ACCEPTED": frozenset(
        {"DELIVERED", "INFERENCE_FAILED", "CANCELLED", "PARTIAL_OUTPUT"}
    ),
    "DELIVERED": frozenset({"RESPONSE_COMMITTED", "RESPONSE_DISCARDED"}),
}
_TERMINAL_KINDS = frozenset(
    {"COMPLETED", "INCOMPLETE", "TOOL_USE", "REFUSAL"}
)
_DELIVERED_EVIDENCE_STATES = frozenset(
    {"DELIVERED", "ACTIVE", "SATISFIED", "SUPERSEDED"}
)


class AttestationIntegrityError(ValueError):
    """The exported bytes or their self-seal are not authoritative."""


@dataclass(frozen=True)
class RejectedRuntimeProof:
    delivery_attempt_id: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "delivery_attempt_id": self.delivery_attempt_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvidenceOwnership:
    evidence_id: str
    feature_id: str
    owner_feature_ids: tuple[str, ...]
    lifecycle: str
    evidence_state_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "feature_id": self.feature_id,
            "owner_feature_ids": list(self.owner_feature_ids),
            "lifecycle": self.lifecycle,
            "evidence_state_hash": self.evidence_state_hash,
        }


@dataclass(frozen=True)
class CanonicalDeliveryAttestation:
    attempt_id: str
    delivery_attempt_id: str
    model_call_id: str
    observation_id: str
    decision_id: str
    decision_context: str
    lifecycle_states: tuple[str, ...]
    evidence: tuple[EvidenceOwnership, ...]
    capsule_hash: str
    provider_payload_hash: str
    provider_response_id_hash: str
    response_hash: str
    terminal_kind: str
    delivery_proven: bool
    response_committed: bool
    source_chain_hash: str
    record_sha256: str
    explicit_acknowledgment: str = "UNKNOWN"
    behavioral_influence: str = "NOT_EVALUATED"
    schema: str = SCHEMA

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "attempt_id": self.attempt_id,
            "delivery_attempt_id": self.delivery_attempt_id,
            "model_call_id": self.model_call_id,
            "observation_id": self.observation_id,
            "decision_id": self.decision_id,
            "decision_context": self.decision_context,
            "lifecycle_states": list(self.lifecycle_states),
            "evidence": [item.as_dict() for item in self.evidence],
            "capsule_hash": self.capsule_hash,
            "provider_payload_hash": self.provider_payload_hash,
            "provider_response_id_hash": self.provider_response_id_hash,
            "response_hash": self.response_hash,
            "terminal_kind": self.terminal_kind,
            "delivery_proven": self.delivery_proven,
            "response_committed": self.response_committed,
            "source_chain_hash": self.source_chain_hash,
            "explicit_acknowledgment": self.explicit_acknowledgment,
            "behavioral_influence": self.behavioral_influence,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "record_sha256": self.record_sha256}


@dataclass(frozen=True)
class RuntimeAttestationLoad:
    journal_present: bool
    integrity_ok: bool
    attestations: tuple[CanonicalDeliveryAttestation, ...]
    rejected: tuple[RejectedRuntimeProof, ...]


@dataclass(frozen=True)
class RuntimeAttestationBundle:
    records: tuple[CanonicalDeliveryAttestation, ...]
    rejected: tuple[RejectedRuntimeProof, ...]
    integrity_ok: bool
    attestation_sha256: str
    schema: str = BUNDLE_SCHEMA

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "integrity_ok": self.integrity_ok,
            "records": [item.as_dict() for item in self.records],
            "rejected": [item.as_dict() for item in self.rejected],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.unsigned_dict(),
            "attestation_sha256": self.attestation_sha256,
        }


class _Reject(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_generation_projection(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove the fields that may advance without changing a generation."""

    projected = dict(payload)
    projected.update(
        {
            "lifecycle": "PENDING",
            "fresh": True,
            "superseded": False,
            "transition_history": [],
            "owner_feature_ids": [],
            # Imported, never re-derived: this reader and the runtime writer must erase the
            # runtime-owned window marker by the SAME rule, or a legitimately advanced window
            # reads here as a rewritten generation. See `projected_decision_window`.
            "decision_window_generation": projected_decision_window(
                payload.get("feature_id"),
                payload.get("mandatory_reason"),
            ),
        }
    )
    return projected


def _owners_are_authorized(
    feature_id: object,
    owners: Sequence[object],
) -> bool:
    """Validate CAP audit owners against the authoritative FACT bindings."""

    if not isinstance(feature_id, str) or not feature_id:
        return False
    for owner in owners:
        if not isinstance(owner, str):
            return False
        authority = CAP_BYTE_OWNER_MECHANISMS.get(owner)
        if authority is None or not any(
            binding.fact_class == feature_id
            for binding in authority.bindings
        ):
            return False
    return True


def _evidence_history_is_valid(
    history: Sequence[object],
    lifecycle: object,
) -> bool:
    """Validate the persisted transition chain against the runtime authority."""

    if not isinstance(lifecycle, str):
        return False
    if not history:
        return lifecycle in {"DISCOVERED", "PENDING"}
    previous_to = ""
    for index, transition in enumerate(history):
        if not isinstance(transition, dict):
            return False
        try:
            from_state = EvidenceLifecycle(transition["from_state"])
            to_state = EvidenceLifecycle(transition["to_state"])
            reason = EvidenceTransitionReason(transition["reason_code"])
        except (KeyError, TypeError, ValueError):
            return False
        if (
            index == 0
            and from_state
            not in {EvidenceLifecycle.DISCOVERED, EvidenceLifecycle.PENDING}
        ):
            return False
        if previous_to and from_state.value != previous_to:
            return False
        if (
            from_state,
            to_state,
        ) not in _EVIDENCE_REASON_TRANSITIONS.get(reason, frozenset()):
            return False
        previous_to = to_state.value
    return previous_to == lifecycle


def _sha256_id(value: str) -> str:
    return _sha256_text(value) if value else ""


def _valid_sha256(value: object, *, allow_empty: bool = False) -> bool:
    if allow_empty and value == "":
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _canonical_payload(raw: object, state_hash: object, reason: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not isinstance(state_hash, str):
        raise _Reject(reason)
    if _sha256_text(raw) != state_hash:
        raise _Reject(reason)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _Reject(reason) from exc
    if not isinstance(parsed, dict) or _canonical_json(parsed) != raw:
        raise _Reject(reason)
    return parsed


def _read_rows(
    connection: sqlite3.Connection,
    delivery_attempt_id: str,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    delivery_rows = connection.execute(
        """
        SELECT journal_sequence, model_call_id, state, capsule_hash,
               provider_payload_hash, response_hash, state_hash, canonical_json
        FROM delivery_journal
        WHERE delivery_attempt_id = ?
        ORDER BY journal_sequence
        """,
        (delivery_attempt_id,),
    ).fetchall()
    compilation_rows = connection.execute(
        """
        SELECT journal_sequence, attempt_id, model_call_id, state_hash,
               canonical_json
        FROM compilation_journal
        WHERE delivery_attempt_id = ?
        ORDER BY journal_sequence
        """,
        (delivery_attempt_id,),
    ).fetchall()
    return delivery_rows, compilation_rows


def _validate_delivery_rows(
    rows: Sequence[tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...]]:
    if not rows:
        raise _Reject("DELIVERY_HISTORY_ABSENT")
    payloads: list[dict[str, Any]] = []
    states: list[str] = []
    row_hashes: list[str] = []
    immutable_identity: tuple[object, object, object] | None = None
    observation_id = ""
    joined_payload_hash = ""
    provider_response_id = ""
    for expected, row in enumerate(rows, start=1):
        if int(row[0]) != expected:
            raise _Reject("DELIVERY_SEQUENCE_INVALID")
        payload = _canonical_payload(
            row[7],
            row[6],
            "DELIVERY_STATE_HASH_INVALID",
        )
        state = str(row[2])
        if (
            payload.get("model_call_id") != str(row[1])
            or payload.get("state") != state
            or payload.get("capsule_hash") != str(row[3])
            or payload.get("provider_payload_hash", "") != str(row[4])
            or payload.get("response_hash", "") != str(row[5])
        ):
            raise _Reject("DELIVERY_COLUMN_CANONICAL_MISMATCH")
        if not _valid_sha256(payload.get("capsule_hash")):
            raise _Reject("CAPSULE_HASH_INVALID")
        current_identity = (
            payload.get("evidence_ids"),
            payload.get("capsule_hash"),
            payload.get("model_call_id"),
        )
        if immutable_identity is None:
            immutable_identity = current_identity
        elif current_identity != immutable_identity:
            raise _Reject("DELIVERY_IDENTITY_REWRITTEN")
        if states and state not in _ALLOWED_NEXT.get(states[-1], frozenset()):
            raise _Reject("DELIVERY_LIFECYCLE_INVALID")
        if not states and state != "SELECTED":
            raise _Reject("DELIVERY_INITIAL_STATE_INVALID")
        current_observation = str(payload.get("observation_id", ""))
        if state != "SELECTED":
            if not current_observation:
                raise _Reject("DELIVERY_OBSERVATION_ABSENT")
            if observation_id and current_observation != observation_id:
                raise _Reject("DELIVERY_OBSERVATION_REWRITTEN")
            observation_id = current_observation
        current_payload_hash = str(
            payload.get("provider_payload_hash", "")
        )
        if state in {
            "JOINED",
            "DISPATCHED",
            "PROVIDER_ACCEPTED",
            "DELIVERED",
            "RESPONSE_COMMITTED",
            "DISPATCH_FAILED",
            "PROVIDER_REJECTED",
            "INFERENCE_FAILED",
            "CANCELLED",
            "PARTIAL_OUTPUT",
            "RESPONSE_DISCARDED",
        }:
            if (
                payload.get("joined_capsule_hash")
                != payload.get("capsule_hash")
                or not _valid_sha256(current_payload_hash)
            ):
                raise _Reject("JOINED_IDENTITY_INVALID")
            if (
                joined_payload_hash
                and current_payload_hash != joined_payload_hash
            ):
                raise _Reject("PROVIDER_PAYLOAD_IDENTITY_REWRITTEN")
            joined_payload_hash = current_payload_hash
        current_response_id = str(
            payload.get("provider_response_id", "")
        )
        if state in {
            "PROVIDER_ACCEPTED",
            "DELIVERED",
            "RESPONSE_COMMITTED",
            "INFERENCE_FAILED",
            "CANCELLED",
            "PARTIAL_OUTPUT",
            "RESPONSE_DISCARDED",
        }:
            if not current_response_id:
                raise _Reject("PROVIDER_RESPONSE_ID_ABSENT")
            if (
                provider_response_id
                and current_response_id != provider_response_id
            ):
                raise _Reject("PROVIDER_RESPONSE_ID_REWRITTEN")
            provider_response_id = current_response_id
        states.append(state)
        payloads.append(payload)
        row_hashes.append(str(row[6]))
    return payloads, tuple(states), tuple(row_hashes)


def _validate_structural_binding(
    compilation: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> None:
    payload_json = compilation.get("bound_provider_payload_json")
    binding = compilation.get("binding")
    if not isinstance(payload_json, str) or not isinstance(binding, dict):
        raise _Reject("JOIN_BINDING_ABSENT")
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise _Reject("PROVIDER_PAYLOAD_CANONICAL_INVALID") from exc
    if _canonical_json(payload) != payload_json:
        raise _Reject("PROVIDER_PAYLOAD_CANONICAL_INVALID")
    payload_hash = _sha256_text(payload_json)
    if (
        payload_hash != delivery.get("provider_payload_hash")
        or binding.get("provider_payload_hash") != payload_hash
        or binding.get("capsule_hash") != delivery.get("capsule_hash")
        or binding.get("model_call_id") != delivery.get("model_call_id")
        or binding.get("observation_id") != delivery.get("observation_id")
        or binding.get("decision_id", "")
        != compilation.get("decision_id", "")
        or binding.get("decision_context")
        != compilation.get("decision_context")
        or binding.get("evidence_ids") != compilation.get("evidence_ids")
        or binding.get("evidence_manifest_hash")
        != compilation.get("evidence_manifest_hash")
    ):
        raise _Reject("JOIN_BINDING_HASH_MISMATCH")
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise _Reject("JOIN_BINDING_LOCATION_INVALID")
    message_index = binding.get("message_index")
    content_index = binding.get("content_index")
    if (
        type(message_index) is not int
        or type(content_index) is not int
        or message_index < 0
        or message_index >= len(messages)
    ):
        raise _Reject("JOIN_BINDING_LOCATION_INVALID")
    message = messages[message_index]
    if not isinstance(message, dict):
        raise _Reject("JOIN_BINDING_LOCATION_INVALID")
    capsule_text = compilation.get("capsule_text")
    content = message.get("content")
    if isinstance(content, str):
        bound_text = content if content_index == 0 else None
    elif (
        isinstance(content, list)
        and content_index < len(content)
        and isinstance(content[content_index], dict)
        and content[content_index].get("type") == "text"
    ):
        bound_text = content[content_index].get("text")
    else:
        bound_text = None
    if bound_text != capsule_text:
        raise _Reject("JOIN_BINDING_LOCATION_INVALID")
    matches = 0
    for candidate in messages:
        if not isinstance(candidate, dict):
            continue
        candidate_content = candidate.get("content")
        if candidate_content == capsule_text:
            matches += 1
        elif isinstance(candidate_content, list):
            matches += sum(
                1
                for block in candidate_content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and block.get("text") == capsule_text
            )
    if matches != 1:
        raise _Reject("JOIN_BINDING_AMBIGUOUS")


def _validate_compilation_rows(
    rows: Sequence[tuple[Any, ...]],
    *,
    attempt_id: str,
    delivery_payloads: Sequence[Mapping[str, Any]],
    delivery_states: Sequence[str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if len(rows) != len(delivery_payloads) - 1:
        raise _Reject("COMPILATION_DELIVERY_LENGTH_MISMATCH")
    row_hashes: list[str] = []
    latest: dict[str, Any] | None = None
    immutable_identity: tuple[object, ...] | None = None
    for expected, (row, delivery) in enumerate(
        zip(rows, delivery_payloads[1:]),
        start=1,
    ):
        if int(row[0]) != expected or str(row[1]) != attempt_id:
            raise _Reject("COMPILATION_SEQUENCE_OR_OWNER_INVALID")
        payload = _canonical_payload(
            row[4],
            row[3],
            "COMPILATION_STATE_HASH_INVALID",
        )
        embedded = payload.get("delivery_attempt")
        if (
            payload.get("state") != "COMPILED"
            or payload.get("model_call_id") != str(row[2])
            or embedded != delivery
            or payload.get("evidence_ids") != delivery.get("evidence_ids")
            or payload.get("capsule_hash") != delivery.get("capsule_hash")
            or payload.get("observation_id") != delivery.get("observation_id")
        ):
            raise _Reject("COMPILATION_DELIVERY_MISMATCH")
        current_identity = (
            payload.get("model_call_id"),
            payload.get("observation_id"),
            payload.get("decision_id", ""),
            payload.get("decision_context"),
            payload.get("evidence_ids"),
            payload.get("capsule_hash"),
            payload.get("rendered_content_hash"),
            payload.get("evidence_manifest_hash"),
        )
        if immutable_identity is None:
            immutable_identity = current_identity
        elif current_identity != immutable_identity:
            raise _Reject("COMPILATION_IDENTITY_REWRITTEN")
        capsule_text = payload.get("capsule_text")
        rendered_hash = payload.get("rendered_content_hash")
        manifest_hash = payload.get("evidence_manifest_hash")
        if (
            not isinstance(capsule_text, str)
            or not capsule_text
            or _sha256_text(capsule_text) != rendered_hash
            or not _valid_sha256(manifest_hash)
        ):
            raise _Reject("CAPSULE_CONTENT_HASH_INVALID")
        expected_capsule_hash = _sha256_text(
            _canonical_json(
                {
                    # IMPORTED, not hardcoded (2026-07-28). This reader RECOMPUTES the capsule
                    # hash to verify it, so a literal here must match the writer's label
                    # exactly -- and when the writer bumped v2->v3 this copy silently failed
                    # every verification, reading as a tamper/identity-rewrite rather than as
                    # a version skew. Four files held this literal; now one owns it.
                    "schema": _DECISION_CAPSULE_SCHEMA,
                    "rendered_content_hash": rendered_hash,
                    "evidence_manifest_hash": manifest_hash,
                }
            )
        )
        if expected_capsule_hash != delivery.get("capsule_hash"):
            raise _Reject("CAPSULE_IDENTITY_HASH_INVALID")
        if delivery_states[expected] in {
            "JOINED",
            "DISPATCHED",
            "PROVIDER_ACCEPTED",
            "DELIVERED",
            "RESPONSE_COMMITTED",
            "DISPATCH_FAILED",
            "PROVIDER_REJECTED",
            "INFERENCE_FAILED",
            "CANCELLED",
            "PARTIAL_OUTPUT",
            "RESPONSE_DISCARDED",
        }:
            _validate_structural_binding(payload, delivery)
        latest = payload
        row_hashes.append(str(row[3]))
    if latest is None:
        raise _Reject("COMPILATION_HISTORY_ABSENT")
    return latest, tuple(row_hashes)


def _load_evidence_ownership(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    evidence_ids: Sequence[str],
    require_delivered: bool,
) -> tuple[tuple[EvidenceOwnership, ...], tuple[str, ...]]:
    records: list[EvidenceOwnership] = []
    source_hashes: list[str] = []
    for evidence_id in evidence_ids:
        rows = connection.execute(
            """
            SELECT journal_sequence, lifecycle, state_hash, canonical_json
            FROM evidence_attempt_journal
            WHERE attempt_id = ? AND evidence_id = ?
            ORDER BY journal_sequence
            """,
            (attempt_id, evidence_id),
        ).fetchall()
        if not rows:
            raise _Reject("EVIDENCE_HISTORY_ABSENT")
        latest: dict[str, Any] | None = None
        latest_hash = ""
        previous_history: list[object] | None = None
        previous_lifecycle = ""
        previous_owners: list[str] | None = None
        previous_window = ""
        generation_projection: dict[str, Any] | None = None
        for expected, row in enumerate(rows, start=1):
            if int(row[0]) != expected:
                raise _Reject("EVIDENCE_SEQUENCE_INVALID")
            payload = _canonical_payload(
                row[3],
                row[2],
                "EVIDENCE_STATE_HASH_INVALID",
            )
            if (
                payload.get("evidence_id") != evidence_id
                or payload.get("lifecycle") != str(row[1])
            ):
                raise _Reject("EVIDENCE_COLUMN_CANONICAL_MISMATCH")
            owners = payload.get("owner_feature_ids", ())
            feature_id = payload.get("feature_id")
            if (
                not isinstance(owners, list)
                or owners != sorted(set(owners))
                or any(
                    not isinstance(owner, str)
                    or not owner.startswith("GT_")
                    for owner in owners
                )
                or not _owners_are_authorized(feature_id, owners)
            ):
                raise _Reject("EVIDENCE_OWNERSHIP_INVALID")
            current_projection = _evidence_generation_projection(payload)
            if generation_projection is None:
                generation_projection = current_projection
            elif current_projection != generation_projection:
                raise _Reject("EVIDENCE_GENERATION_REWRITTEN")
            if latest is not None:
                if feature_id != latest.get("feature_id"):
                    raise _Reject("EVIDENCE_OWNERSHIP_REWRITTEN")
                if (
                    previous_owners is None
                    or not set(previous_owners).issubset(owners)
                ):
                    raise _Reject("EVIDENCE_OWNERSHIP_REWRITTEN")
            history = payload.get("transition_history")
            if (
                not isinstance(history, list)
                or not _evidence_history_is_valid(
                    history,
                    payload.get("lifecycle"),
                )
            ):
                raise _Reject("EVIDENCE_TRANSITION_HISTORY_INVALID")
            if previous_history is not None:
                lifecycle_transition = (
                    len(history) != len(previous_history) + 1
                    or history[:-1] != previous_history
                    or not isinstance(history[-1], dict)
                    or history[-1].get("from_state")
                    != previous_lifecycle
                    or history[-1].get("to_state")
                    != payload.get("lifecycle")
                )
                owner_enrichment = (
                    history == previous_history
                    and payload.get("lifecycle") == previous_lifecycle
                    and previous_owners is not None
                    and set(previous_owners) < set(owners)
                )
                # THE THIRD IN-BAND NO-OP: the decision window OPENING over a record that has
                # not otherwise moved. `decision_window_generation()` stamps the marker onto a
                # still-PENDING record, which advances no lifecycle state, appends no history
                # entry, and enriches no owner -- so it matched neither admitted form and the
                # reader rejected a correct history. Observed on run 30390877219, 5/5 tasks,
                # journal seq1->seq2 of the task-start `obligations` capsule:
                #   seq1 PENDING owners=[] history=[] window=''
                #   seq2 PENDING owners=[] history=[] window='GT-W-7a6db489b9fec7fd'
                # while seq3..seq6 were a textbook PENDING->READY->RELEASED->DELIVERED->ACTIVE
                # chain. This is the same blind spot as `projected_decision_window`, one guard
                # further down: the window is runtime-owned scheduling state, and neither guard
                # had a name for it moving on its own.
                #
                # STRICTLY ONE DIRECTION. Only unset -> assigned is admitted. A marker changing
                # to a DIFFERENT marker, or being cleared, stays a rejection: those would be a
                # record migrating between decision windows, which is exactly the rewriting this
                # guard exists to catch. Everything else must be byte-identical.
                window = str(payload.get("decision_window_generation") or "")
                window_assignment = (
                    history == previous_history
                    and payload.get("lifecycle") == previous_lifecycle
                    and previous_owners is not None
                    and set(previous_owners) == set(owners)
                    and previous_window == ""
                    and window.startswith("GT-W-")
                )
                if (
                    lifecycle_transition
                    and not owner_enrichment
                    and not window_assignment
                ):
                    raise _Reject("EVIDENCE_TRANSITION_HISTORY_INVALID")
            previous_window = str(
                payload.get("decision_window_generation") or ""
            )
            previous_history = history
            previous_lifecycle = str(payload.get("lifecycle", ""))
            previous_owners = owners
            latest = payload
            latest_hash = str(row[2])
            source_hashes.append(latest_hash)
        assert latest is not None
        lifecycle = str(latest.get("lifecycle", ""))
        if require_delivered and lifecycle not in _DELIVERED_EVIDENCE_STATES:
            raise _Reject("DELIVERY_EVIDENCE_LIFECYCLE_UNPROVEN")
        if require_delivered and not any(
            isinstance(transition, dict)
            and transition.get("from_state") == "RELEASED"
            and transition.get("to_state") == "DELIVERED"
            and transition.get("reason_code")
            == "PROVIDER_TERMINAL_DELIVERY_PROVEN"
            for transition in latest.get("transition_history", ())
        ):
            raise _Reject("DELIVERY_EVIDENCE_TRANSITION_UNPROVEN")
        feature_id = latest.get("feature_id")
        owners = latest.get("owner_feature_ids", ())
        if (
            not isinstance(feature_id, str)
            or not feature_id
            or not isinstance(owners, list)
            or owners != sorted(set(owners))
            or not _owners_are_authorized(feature_id, owners)
        ):
            raise _Reject("EVIDENCE_OWNERSHIP_INVALID")
        records.append(
            EvidenceOwnership(
                evidence_id=evidence_id,
                feature_id=feature_id,
                owner_feature_ids=tuple(owners),
                lifecycle=lifecycle,
                evidence_state_hash=latest_hash,
            )
        )
    return tuple(records), tuple(source_hashes)


def _build_attestation(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    delivery_attempt_id: str,
    expected_model_call_id: str,
) -> CanonicalDeliveryAttestation:
    delivery_rows, compilation_rows = _read_rows(
        connection,
        delivery_attempt_id,
    )
    delivery_payloads, states, delivery_hashes = _validate_delivery_rows(
        delivery_rows
    )
    latest_delivery = delivery_payloads[-1]
    if latest_delivery.get("model_call_id") != expected_model_call_id:
        raise _Reject("DELIVERY_ATTEMPT_IDENTITY_MISMATCH")
    latest_compilation, compilation_hashes = _validate_compilation_rows(
        compilation_rows,
        attempt_id=attempt_id,
        delivery_payloads=delivery_payloads,
        delivery_states=states,
    )
    evidence_ids = latest_delivery.get("evidence_ids")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or evidence_ids != latest_compilation.get("evidence_ids")
    ):
        raise _Reject("EVIDENCE_IDENTITY_MISMATCH")
    delivery_proven = (
        len(states) >= len(_SUCCESS_PREFIX)
        and tuple(states[: len(_SUCCESS_PREFIX)]) == _SUCCESS_PREFIX
        and latest_delivery.get("state")
        in {"DELIVERED", "RESPONSE_COMMITTED", "RESPONSE_DISCARDED"}
        and latest_delivery.get("terminal_kind") in _TERMINAL_KINDS
        and bool(latest_delivery.get("provider_response_id"))
    )
    evidence, evidence_hashes = _load_evidence_ownership(
        connection,
        attempt_id=attempt_id,
        evidence_ids=evidence_ids,
        require_delivered=delivery_proven,
    )
    response_committed = states[-1] == "RESPONSE_COMMITTED"
    if response_committed and not _valid_sha256(
        latest_delivery.get("response_hash")
    ):
        raise _Reject("RESPONSE_COMMIT_HASH_INVALID")
    provider_hash = str(latest_delivery.get("provider_payload_hash", ""))
    if delivery_proven and not _valid_sha256(provider_hash):
        raise _Reject("PROVIDER_PAYLOAD_HASH_INVALID")
    source_chain_hash = _sha256_text(
        _canonical_json(
            {
                "delivery": list(delivery_hashes),
                "compilation": list(compilation_hashes),
                "evidence": list(evidence_hashes),
            }
        )
    )
    unsigned = {
        "schema": SCHEMA,
        "attempt_id": attempt_id,
        "delivery_attempt_id": delivery_attempt_id,
        "model_call_id": str(latest_delivery.get("model_call_id", "")),
        "observation_id": str(latest_delivery.get("observation_id", "")),
        "decision_id": str(latest_compilation.get("decision_id", "")),
        "decision_context": str(
            latest_compilation.get("decision_context", "")
        ),
        "lifecycle_states": list(states),
        "evidence": [item.as_dict() for item in evidence],
        "capsule_hash": str(latest_delivery.get("capsule_hash", "")),
        "provider_payload_hash": provider_hash,
        "provider_response_id_hash": _sha256_id(
            str(latest_delivery.get("provider_response_id", ""))
        ),
        "response_hash": str(latest_delivery.get("response_hash", "")),
        "terminal_kind": str(latest_delivery.get("terminal_kind") or ""),
        "delivery_proven": delivery_proven,
        "response_committed": response_committed,
        "source_chain_hash": source_chain_hash,
        "explicit_acknowledgment": "UNKNOWN",
        "behavioral_influence": "NOT_EVALUATED",
    }
    return CanonicalDeliveryAttestation(
        attempt_id=attempt_id,
        delivery_attempt_id=delivery_attempt_id,
        model_call_id=unsigned["model_call_id"],
        observation_id=unsigned["observation_id"],
        decision_id=unsigned["decision_id"],
        decision_context=unsigned["decision_context"],
        lifecycle_states=states,
        evidence=evidence,
        capsule_hash=unsigned["capsule_hash"],
        provider_payload_hash=provider_hash,
        provider_response_id_hash=unsigned["provider_response_id_hash"],
        response_hash=unsigned["response_hash"],
        terminal_kind=unsigned["terminal_kind"],
        delivery_proven=delivery_proven,
        response_committed=response_committed,
        source_chain_hash=source_chain_hash,
        record_sha256=_sha256_text(_canonical_json(unsigned)),
    )


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def load_runtime_attestations(
    journal_path: str | os.PathLike[str],
    *,
    attempt_id: str | None = None,
) -> RuntimeAttestationLoad:
    """Validate and project one canonical runtime journal without writing it."""

    path = Path(journal_path)
    if not path.is_file():
        return RuntimeAttestationLoad(False, True, (), ())
    try:
        connection = _readonly_connection(path)
    except sqlite3.Error:
        return RuntimeAttestationLoad(
            True,
            False,
            (),
            (RejectedRuntimeProof("", "JOURNAL_OPEN_FAILED"),),
        )
    with closing(connection):
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        except sqlite3.Error:
            return RuntimeAttestationLoad(
                True,
                False,
                (),
                (RejectedRuntimeProof("", "JOURNAL_SCHEMA_READ_FAILED"),),
            )
        missing = sorted(_REQUIRED_TABLES - tables)
        if missing:
            return RuntimeAttestationLoad(
                True,
                False,
                (),
                (
                    RejectedRuntimeProof(
                        "",
                        "JOURNAL_SCHEMA_INCOMPLETE:" + ",".join(missing),
                    ),
                ),
            )
        query = (
            "SELECT delivery_attempt_id, model_call_id, attempt_id "
            "FROM delivery_attempts"
        )
        params: tuple[object, ...] = ()
        if attempt_id is not None:
            query += " WHERE attempt_id = ?"
            params = (attempt_id,)
        query += " ORDER BY attempt_id, delivery_attempt_id"
        try:
            identities = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return RuntimeAttestationLoad(
                True,
                False,
                (),
                (RejectedRuntimeProof("", "JOURNAL_IDENTITY_READ_FAILED"),),
            )
        attestations: list[CanonicalDeliveryAttestation] = []
        rejected: list[RejectedRuntimeProof] = []
        for (
            delivery_attempt_id,
            expected_model_call_id,
            owner_attempt_id,
        ) in identities:
            delivery_id = str(delivery_attempt_id)
            owner = str(owner_attempt_id)
            if not owner:
                rejected.append(
                    RejectedRuntimeProof(
                        delivery_id,
                        "ATTEMPT_OWNER_ABSENT",
                    )
                )
                continue
            try:
                attestations.append(
                    _build_attestation(
                        connection,
                        attempt_id=owner,
                        delivery_attempt_id=delivery_id,
                        expected_model_call_id=str(
                            expected_model_call_id
                        ),
                    )
                )
            except (_Reject, sqlite3.Error) as exc:
                reason = (
                    exc.reason
                    if isinstance(exc, _Reject)
                    else "JOURNAL_QUERY_FAILED"
                )
                rejected.append(RejectedRuntimeProof(delivery_id, reason))
        return RuntimeAttestationLoad(
            journal_present=True,
            integrity_ok=not rejected,
            attestations=(
                tuple(attestations) if not rejected else ()
            ),
            rejected=tuple(rejected),
        )


def _build_bundle(load: RuntimeAttestationLoad) -> RuntimeAttestationBundle:
    unsigned = {
        "schema": BUNDLE_SCHEMA,
        "integrity_ok": load.integrity_ok,
        "records": [item.as_dict() for item in load.attestations],
        "rejected": [item.as_dict() for item in load.rejected],
    }
    return RuntimeAttestationBundle(
        records=load.attestations,
        rejected=load.rejected,
        integrity_ok=load.integrity_ok,
        attestation_sha256=_sha256_text(_canonical_json(unsigned)),
    )


def export_runtime_attestations(
    journal_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    attempt_id: str | None = None,
) -> RuntimeAttestationBundle:
    """Write one self-sealed canonical proof bundle without overwriting."""

    load = load_runtime_attestations(journal_path, attempt_id=attempt_id)
    if not load.journal_present:
        raise AttestationIntegrityError("canonical runtime journal is absent")
    bundle = _build_bundle(load)
    encoded = (_canonical_json(bundle.as_dict()) + "\n").encode("utf-8")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            output.unlink()
        except OSError:
            pass
        raise
    return bundle


def _record_from_dict(raw: Mapping[str, Any]) -> CanonicalDeliveryAttestation:
    try:
        if (
            type(raw.get("delivery_proven")) is not bool
            or type(raw.get("response_committed")) is not bool
        ):
            raise AttestationIntegrityError(
                "attestation boolean shape invalid"
            )
        evidence_raw = raw.get("evidence")
        if not isinstance(evidence_raw, list):
            raise AttestationIntegrityError(
                "attestation evidence shape invalid"
            )
        evidence = tuple(
            EvidenceOwnership(
                evidence_id=str(item["evidence_id"]),
                feature_id=str(item["feature_id"]),
                owner_feature_ids=tuple(
                    item.get("owner_feature_ids", ())
                ),
                lifecycle=str(item["lifecycle"]),
                evidence_state_hash=str(item["evidence_state_hash"]),
            )
            for item in evidence_raw
            if isinstance(item, dict)
        )
        if len(evidence) != len(evidence_raw):
            raise AttestationIntegrityError(
                "attestation evidence shape invalid"
            )
        record = CanonicalDeliveryAttestation(
            attempt_id=str(raw["attempt_id"]),
            delivery_attempt_id=str(raw["delivery_attempt_id"]),
            model_call_id=str(raw["model_call_id"]),
            observation_id=str(raw["observation_id"]),
            decision_id=str(raw["decision_id"]),
            decision_context=str(raw["decision_context"]),
            lifecycle_states=tuple(raw["lifecycle_states"]),
            evidence=evidence,
            capsule_hash=str(raw["capsule_hash"]),
            provider_payload_hash=str(raw["provider_payload_hash"]),
            provider_response_id_hash=str(
                raw["provider_response_id_hash"]
            ),
            response_hash=str(raw["response_hash"]),
            terminal_kind=str(raw["terminal_kind"]),
            delivery_proven=bool(raw["delivery_proven"]),
            response_committed=bool(raw["response_committed"]),
            source_chain_hash=str(raw["source_chain_hash"]),
            record_sha256=str(raw["record_sha256"]),
            explicit_acknowledgment=str(
                raw["explicit_acknowledgment"]
            ),
            behavioral_influence=str(raw["behavioral_influence"]),
            schema=str(raw["schema"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, AttestationIntegrityError):
            raise
        raise AttestationIntegrityError(
            "attestation record shape invalid"
        ) from exc
    delivery_expected = (
        len(record.lifecycle_states) >= len(_SUCCESS_PREFIX)
        and record.lifecycle_states[: len(_SUCCESS_PREFIX)]
        == _SUCCESS_PREFIX
        and record.lifecycle_states[-1]
        in {"DELIVERED", "RESPONSE_COMMITTED", "RESPONSE_DISCARDED"}
        and record.terminal_kind in _TERMINAL_KINDS
        and bool(record.provider_response_id_hash)
    )
    state_chain_valid = bool(record.lifecycle_states) and (
        record.lifecycle_states[0] == "SELECTED"
    ) and all(
        current in _ALLOWED_NEXT.get(previous, frozenset())
        for previous, current in zip(
            record.lifecycle_states,
            record.lifecycle_states[1:],
        )
    )
    if (
        record.schema != SCHEMA
        or not record.attempt_id
        or not record.delivery_attempt_id
        or not record.model_call_id
        or not record.observation_id
        or not record.evidence
        or not state_chain_valid
        or record.explicit_acknowledgment != "UNKNOWN"
        or record.behavioral_influence != "NOT_EVALUATED"
        or record.delivery_proven != delivery_expected
        or record.response_committed
        != (record.lifecycle_states[-1:] == ("RESPONSE_COMMITTED",))
        or not _valid_sha256(record.capsule_hash)
        or not _valid_sha256(record.source_chain_hash)
        or (
            bool(record.provider_payload_hash)
            and not _valid_sha256(record.provider_payload_hash)
        )
        or (
            bool(record.provider_response_id_hash)
            and not _valid_sha256(record.provider_response_id_hash)
        )
        or (
            record.delivery_proven
            and (
                not _valid_sha256(record.provider_payload_hash)
                or not _valid_sha256(
                    record.provider_response_id_hash
                )
            )
        )
        or (
            record.response_committed
            and not _valid_sha256(record.response_hash)
        )
        or any(
            not item.evidence_id
            or not item.feature_id
            or item.owner_feature_ids
            != tuple(sorted(set(item.owner_feature_ids)))
            or any(
                not owner.startswith("GT_")
                for owner in item.owner_feature_ids
            )
            or not _valid_sha256(item.evidence_state_hash)
            for item in record.evidence
        )
        or _sha256_text(_canonical_json(record.unsigned_dict()))
        != record.record_sha256
    ):
        raise AttestationIntegrityError("record seal invalid")
    return record


def read_runtime_attestation_bundle(
    path: str | os.PathLike[str],
) -> RuntimeAttestationBundle:
    """Read exact canonical export bytes and verify both seal levels."""

    try:
        encoded = Path(path).read_bytes()
        text = encoded.decode("utf-8")
        if not text.endswith("\n"):
            raise AttestationIntegrityError(
                "attestation canonical framing invalid"
            )
        raw = json.loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, AttestationIntegrityError):
            raise
        raise AttestationIntegrityError(
            "attestation canonical bytes invalid"
        ) from exc
    if not isinstance(raw, dict) or _canonical_json(raw) + "\n" != text:
        raise AttestationIntegrityError("attestation canonical bytes invalid")
    seal = raw.get("attestation_sha256")
    unsigned = {
        key: value
        for key, value in raw.items()
        if key != "attestation_sha256"
    }
    if (
        raw.get("schema") != BUNDLE_SCHEMA
        or not isinstance(seal, str)
        or _sha256_text(_canonical_json(unsigned)) != seal
    ):
        raise AttestationIntegrityError("attestation bundle seal invalid")
    records_raw = raw.get("records")
    rejected_raw = raw.get("rejected")
    if not isinstance(records_raw, list) or not isinstance(rejected_raw, list):
        raise AttestationIntegrityError("attestation bundle shape invalid")
    records = tuple(_record_from_dict(item) for item in records_raw)
    rejected = tuple(
        RejectedRuntimeProof(
            delivery_attempt_id=str(item["delivery_attempt_id"]),
            reason=str(item["reason"]),
        )
        for item in rejected_raw
        if isinstance(item, dict)
    )
    if len(rejected) != len(rejected_raw):
        raise AttestationIntegrityError("attestation rejection shape invalid")
    integrity_ok = bool(raw.get("integrity_ok"))
    if integrity_ok != (not rejected) or (rejected and records):
        raise AttestationIntegrityError(
            "attestation bundle integrity projection invalid"
        )
    return RuntimeAttestationBundle(
        records=records,
        rejected=rejected,
        integrity_ok=integrity_ok,
        attestation_sha256=seal,
    )


def runtime_attestation_diagnostic(
    task_dir: str | os.PathLike[str],
) -> dict[str, Any] | None:
    """Return an additive metrics diagnostic, or ``None`` when legacy-only."""

    root = Path(task_dir)
    paths = sorted(root.rglob("gt_reasoning_runtime.sqlite3"))
    if not paths:
        return None
    if len(paths) != 1:
        return {
            "schema": SCHEMA,
            "journal_present": True,
            "integrity_ok": False,
            "delivered_count": 0,
            "response_committed_count": 0,
            "rejected": [
                {
                    "delivery_attempt_id": "",
                    "reason": "MULTIPLE_CANONICAL_JOURNALS",
                }
            ],
            "explicit_acknowledgment": "UNMEASURED",
            "behavioral_influence": "UNMEASURED",
        }
    loaded = load_runtime_attestations(paths[0])
    bundle = _build_bundle(loaded)
    return {
        "schema": SCHEMA,
        "journal_present": loaded.journal_present,
        "integrity_ok": loaded.integrity_ok,
        "attestation_sha256": bundle.attestation_sha256,
        "delivered_count": sum(
            item.delivery_proven for item in loaded.attestations
        ),
        "response_committed_count": sum(
            item.response_committed for item in loaded.attestations
        ),
        "records": [item.as_dict() for item in loaded.attestations],
        "rejected": [item.as_dict() for item in loaded.rejected],
        "explicit_acknowledgment": "UNMEASURED",
        "behavioral_influence": "UNMEASURED",
    }


__all__ = [
    "AttestationIntegrityError",
    "CanonicalDeliveryAttestation",
    "EvidenceOwnership",
    "RejectedRuntimeProof",
    "RuntimeAttestationBundle",
    "RuntimeAttestationLoad",
    "export_runtime_attestations",
    "load_runtime_attestations",
    "read_runtime_attestation_bundle",
    "runtime_attestation_diagnostic",
]
