"""Canonical contracts for deterministic action-bound observation compilation.

This module is a render-neutral adapter over GroundTruth's existing authorities:

* :class:`EvidenceEnvelope` remains the producer contract.
* :class:`RevisionVector` remains the runtime revision authority.
* :class:`DeliveryAttempt` and ``MiniSweProviderBoundary`` remain the delivery
  state machine and provider-bound truth authority.

The values below bind a chosen action to those authorities and make the
interception decision auditable.  They do not render evidence, mutate an
observation, dispatch a provider request, or infer planner intent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Sequence

from .evidence_envelope import EvidenceEnvelope, to_dict as envelope_to_dict
from .reasoning_runtime import DeliveryAttempt, DeliveryState, RevisionVector


CONFIGURATION_BINDING_SCHEMA = "gt.configuration_binding.v1"
REPOSITORY_SNAPSHOT_SCHEMA = "gt.repository_snapshot.v1"
ACTION_REQUEST_SCHEMA = "gt.action_request.v1"
EVIDENCE_ARTIFACT_SCHEMA = "gt.evidence_artifact.v1"
INTERCEPTION_DECISION_SCHEMA = "gt.interception_decision.v1"
DELIVERY_RECEIPT_SCHEMA = "gt.delivery_receipt.v1"


class ActionKind(str, Enum):
    SHELL = "shell"
    EXACT_LITERAL_SEARCH = "exact_literal_search"
    SYNTAX_QUERY = "syntax_query"
    PATCH_IMPACT = "patch_impact"
    RUN_VERIFICATION = "run_verification"
    VERIFICATION_STATUS = "verification_status"
    SUBMIT = "submit"


class RequestedFidelity(str, Enum):
    EXACT = "exact"
    SOUND_OVERAPPROX = "sound_overapprox"
    EXECUTION_SPECIFIC = "execution_specific"
    RAW = "raw"


class EvidenceSemantics(str, Enum):
    EXACT = "exact"
    SOUND_OVERAPPROX = "sound_overapprox"
    EXECUTION_SPECIFIC = "execution_specific"
    INCOMPLETE = "incomplete"


class Coverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class InterceptionMode(str, Enum):
    PASS_THROUGH = "PASS_THROUGH"
    AUGMENT = "AUGMENT"
    REPLACE = "REPLACE"
    REWRITE = "REWRITE"
    SUPPRESS = "SUPPRESS"


def _canonical_data(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, EvidenceEnvelope):
        return envelope_to_dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return _canonical_data(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_data(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value is not canonical-JSON encodable: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes for a compiler carrier or source envelope."""

    try:
        return json.dumps(
            _canonical_data(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"canonical serialization failed: {exc}") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _is_sha256(value: object, *, allow_empty: bool = False) -> bool:
    if allow_empty and value == "":
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _canonical_json_value(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise TypeError("hash input must be bytes")
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ConfigurationBinding:
    schema: str
    configuration_id: str
    inputs_sha256: str
    language_manifest_sha256: str
    build_system: str


@dataclass(frozen=True)
class RepositorySnapshot:
    schema: str
    repository_id: str
    root_sha256: str
    git_revision: str
    dirty_diff_sha256: str
    working_tree_sha256: str
    revisions: RevisionVector
    configuration: ConfigurationBinding


@dataclass(frozen=True, order=True)
class SourceAnchor:
    path: str
    line: int
    column: int = 0


@dataclass(frozen=True)
class ActionRequest:
    schema: str
    action_id: str
    kind: ActionKind
    arguments_json: str
    repository_snapshot: RepositorySnapshot
    repository_snapshot_sha256: str
    configuration_sha256: str
    requested_fidelity: RequestedFidelity
    original_shell_form: str = ""

    @classmethod
    def build(
        cls,
        *,
        action_id: str,
        kind: ActionKind,
        arguments: Mapping[str, Any],
        snapshot: RepositorySnapshot,
        requested_fidelity: RequestedFidelity,
        original_shell_form: str = "",
    ) -> "ActionRequest":
        if not isinstance(arguments, Mapping):
            raise TypeError("action arguments must be a mapping")
        request = cls(
            schema=ACTION_REQUEST_SCHEMA,
            action_id=action_id,
            kind=kind,
            arguments_json=_canonical_json_value(arguments),
            repository_snapshot=snapshot,
            repository_snapshot_sha256=canonical_sha256(snapshot),
            configuration_sha256=canonical_sha256(snapshot.configuration),
            requested_fidelity=requested_fidelity,
            original_shell_form=original_shell_form,
        )
        errors = validate(request)
        if errors:
            raise ValueError("action request is invalid: " + "|".join(errors))
        return request

    @property
    def arguments(self) -> Mapping[str, Any]:
        decoded = json.loads(self.arguments_json)
        if not isinstance(decoded, dict):  # guarded by validate; fail loudly if corrupted
            raise ValueError("arguments_json does not contain an object")
        return decoded


@dataclass(frozen=True)
class EvidenceArtifact:
    schema: str
    artifact_id: str
    action_id: str
    request_sha256: str
    envelope_sha256: str
    producer: str
    producer_version: str
    snapshot_sha256: str
    configuration_sha256: str
    producer_revision: str
    semantics: EvidenceSemantics
    direct_answer_json: str
    anchors: tuple[SourceAnchor, ...]
    witnesses: tuple[str, ...]
    coverage: Coverage
    ambiguity: tuple[str, ...]
    omissions: tuple[str, ...]
    raw_fallback_sha256: str

    @property
    def direct_answer(self) -> Any:
        return json.loads(self.direct_answer_json)


@dataclass(frozen=True)
class InterceptionDecision:
    schema: str
    action_id: str
    action_request_sha256: str
    mode: InterceptionMode
    artifact_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    raw_result_required: bool


@dataclass(frozen=True)
class DeliveryReceipt:
    schema: str
    action_request_sha256: str
    repository_snapshot_sha256: str
    interception_decision_sha256: str
    raw_result_sha256: str
    transformation_version: str
    transformation_inputs_sha256: str
    final_observation_sha256: str
    delivery_state: str
    model_call_id: str
    observation_id: str
    provider_payload_sha256: str
    provider_response_id: str
    provider_response_sha256: str
    immediate_next_action_sha256: str


def _artifact_identity_payload(artifact: EvidenceArtifact) -> Mapping[str, Any]:
    data = asdict(artifact)
    data.pop("artifact_id")
    return {"schema": EVIDENCE_ARTIFACT_SCHEMA, "artifact": data}


def _artifact_identity(artifact: EvidenceArtifact) -> str:
    return canonical_sha256(_artifact_identity_payload(artifact))


def artifact_from_envelope(
    *,
    request: ActionRequest,
    envelope: EvidenceEnvelope,
    producer_version: str,
    semantics: EvidenceSemantics,
    direct_answer: Any,
    coverage: Coverage,
    witnesses: Sequence[str] = (),
    ambiguity: Sequence[str] = (),
    omissions: Sequence[str] = (),
    raw_fallback: bytes,
) -> EvidenceArtifact:
    """Translate a producer envelope without acquiring producer authority.

    The envelope's canonical hash and freshness token are retained.  This
    function never changes the envelope, renders it, or claims its delivery.
    """

    anchors = tuple(
        SourceAnchor(path=str(path), line=int(line), column=0)
        for path, line in envelope.provenance
    )
    artifact = EvidenceArtifact(
        schema=EVIDENCE_ARTIFACT_SCHEMA,
        artifact_id="",
        action_id=request.action_id,
        request_sha256=canonical_sha256(request),
        envelope_sha256=canonical_sha256(envelope),
        producer=envelope.producer,
        producer_version=producer_version,
        snapshot_sha256=request.repository_snapshot_sha256,
        configuration_sha256=request.configuration_sha256,
        producer_revision=envelope.valid_until,
        semantics=semantics,
        direct_answer_json=_canonical_json_value(direct_answer),
        anchors=anchors,
        witnesses=tuple(str(item) for item in witnesses),
        coverage=coverage,
        ambiguity=tuple(str(item) for item in ambiguity),
        omissions=tuple(str(item) for item in omissions),
        raw_fallback_sha256=_sha256_bytes(raw_fallback),
    )
    artifact = replace(artifact, artifact_id=_artifact_identity(artifact))
    errors = validate(artifact)
    if errors:
        raise ValueError("evidence artifact is invalid: " + "|".join(errors))
    return artifact


_TYPED_REPLACEABLE_ACTIONS = frozenset(
    {
        ActionKind.EXACT_LITERAL_SEARCH,
        ActionKind.SYNTAX_QUERY,
        ActionKind.VERIFICATION_STATUS,
    }
)


def _expected_producer_revision(request: ActionRequest) -> str:
    revisions = request.repository_snapshot.revisions
    if request.kind in {
        ActionKind.EXACT_LITERAL_SEARCH,
        ActionKind.SYNTAX_QUERY,
        ActionKind.PATCH_IMPACT,
    }:
        return revisions.repository_content
    if request.kind is ActionKind.VERIFICATION_STATUS:
        return revisions.runtime_evidence
    return revisions.graph


def evaluate_interception(
    request: ActionRequest,
    artifacts: Sequence[EvidenceArtifact],
) -> InterceptionDecision:
    """Choose the most permissive mechanically justified interception mode."""

    request_hash = canonical_sha256(request)
    ordered = tuple(sorted(artifacts, key=lambda item: item.artifact_id))
    if request.kind is ActionKind.SHELL:
        return InterceptionDecision(
            INTERCEPTION_DECISION_SCHEMA,
            request.action_id,
            request_hash,
            InterceptionMode.PASS_THROUGH,
            tuple(item.artifact_id for item in ordered),
            ("UNTYPED_ACTION",),
            True,
        )
    if not ordered:
        return InterceptionDecision(
            INTERCEPTION_DECISION_SCHEMA,
            request.action_id,
            request_hash,
            InterceptionMode.PASS_THROUGH,
            (),
            ("NO_EVIDENCE",),
            True,
        )

    reasons: list[str] = []
    if len({item.artifact_id for item in ordered}) != len(ordered):
        reasons.append("DUPLICATE_ARTIFACT")
    for artifact in ordered:
        if validate(artifact):
            reasons.append("ARTIFACT_CONTRACT_INVALID")
        if artifact.action_id != request.action_id:
            reasons.append("artifact:action_id:mismatch")
        if artifact.request_sha256 != request_hash:
            reasons.append("REQUEST_MISMATCH")
        if artifact.snapshot_sha256 != request.repository_snapshot_sha256:
            reasons.append("STALE_SNAPSHOT")
        if artifact.configuration_sha256 != request.configuration_sha256:
            reasons.append("STALE_CONFIGURATION")
        if artifact.producer_revision != _expected_producer_revision(request):
            reasons.append("PRODUCER_REVISION_STALE")
        if artifact.semantics is not EvidenceSemantics.EXACT:
            reasons.append("SEMANTICS_NOT_EXACT")
        if artifact.coverage is not Coverage.COMPLETE:
            reasons.append("COVERAGE_NOT_COMPLETE")
        if artifact.ambiguity:
            reasons.append("AMBIGUOUS_EVIDENCE")
        if artifact.omissions:
            reasons.append("EVIDENCE_HAS_OMISSIONS")

    if request.kind is ActionKind.RUN_VERIFICATION or any(
        item.semantics is EvidenceSemantics.EXECUTION_SPECIFIC for item in ordered
    ):
        reasons.append("RAW_DIAGNOSTICS_REQUIRED")
    if request.requested_fidelity in {
        RequestedFidelity.RAW,
        RequestedFidelity.EXECUTION_SPECIFIC,
    }:
        reasons.append("REQUEST_REQUIRES_RAW")
    if request.kind not in _TYPED_REPLACEABLE_ACTIONS:
        reasons.append("ACTION_NOT_REPLACEABLE")

    reasons = list(dict.fromkeys(reasons))
    mode = InterceptionMode.REPLACE if not reasons else InterceptionMode.AUGMENT
    return InterceptionDecision(
        schema=INTERCEPTION_DECISION_SCHEMA,
        action_id=request.action_id,
        action_request_sha256=request_hash,
        mode=mode,
        artifact_ids=tuple(item.artifact_id for item in ordered),
        reason_codes=tuple(reasons) if reasons else ("EXACT_COMPLETE_EQUIVALENCE",),
        raw_result_required=mode is not InterceptionMode.REPLACE,
    )


def receipt_from_delivery_attempt(
    *,
    request: ActionRequest,
    decision: InterceptionDecision,
    attempt: DeliveryAttempt,
    raw_result: bytes,
    final_observation: bytes,
    transformation_version: str,
    transformation_inputs: Sequence[str],
    immediate_next_action_sha256: str,
) -> DeliveryReceipt:
    """Project an existing provider-bound delivery attempt into compiler lineage."""

    if attempt.state is not DeliveryState.RESPONSE_COMMITTED:
        raise ValueError("receipt requires a response-committed delivery attempt")
    receipt = DeliveryReceipt(
        schema=DELIVERY_RECEIPT_SCHEMA,
        action_request_sha256=canonical_sha256(request),
        repository_snapshot_sha256=request.repository_snapshot_sha256,
        interception_decision_sha256=canonical_sha256(decision),
        raw_result_sha256=_sha256_bytes(raw_result),
        transformation_version=transformation_version,
        transformation_inputs_sha256=canonical_sha256(tuple(transformation_inputs)),
        final_observation_sha256=_sha256_bytes(final_observation),
        delivery_state=attempt.state.value,
        model_call_id=attempt.model_call_id,
        observation_id=attempt.observation_id,
        provider_payload_sha256=attempt.provider_payload_hash,
        provider_response_id=attempt.provider_response_id,
        provider_response_sha256=attempt.response_hash,
        immediate_next_action_sha256=immediate_next_action_sha256,
    )
    errors = validate(receipt)
    if errors:
        raise ValueError("delivery receipt is invalid: " + "|".join(errors))
    return receipt


def validate(value: object) -> tuple[str, ...]:
    """Return deterministic contract violations without coercing corrupt values."""

    errors: list[str] = []
    if isinstance(value, ConfigurationBinding):
        if value.schema != CONFIGURATION_BINDING_SCHEMA:
            errors.append("configuration:schema:unsupported")
        if not _nonempty_text(value.configuration_id):
            errors.append("configuration:configuration_id:invalid")
        if not _is_sha256(value.inputs_sha256):
            errors.append("configuration:inputs_sha256:invalid")
        if not _is_sha256(value.language_manifest_sha256):
            errors.append("configuration:language_manifest_sha256:invalid")
        if not _nonempty_text(value.build_system):
            errors.append("configuration:build_system:invalid")
    elif isinstance(value, RepositorySnapshot):
        if value.schema != REPOSITORY_SNAPSHOT_SCHEMA:
            errors.append("snapshot:schema:unsupported")
        if not _nonempty_text(value.repository_id):
            errors.append("snapshot:repository_id:invalid")
        for field in ("root_sha256", "dirty_diff_sha256", "working_tree_sha256"):
            if not _is_sha256(getattr(value, field)):
                errors.append(f"snapshot:{field}:invalid")
        if not _nonempty_text(value.git_revision):
            errors.append("snapshot:git_revision:invalid")
        if not isinstance(value.revisions, RevisionVector):
            errors.append("snapshot:revisions:wrong_type")
        errors.extend(validate(value.configuration))
    elif isinstance(value, SourceAnchor):
        path = value.path.replace("\\", "/") if isinstance(value.path, str) else ""
        if (
            not path
            or path != value.path
            or path.startswith("/")
            or (len(path) > 1 and path[1] == ":")
            or ".." in path.split("/")
        ):
            errors.append("anchor:path:invalid")
        if type(value.line) is not int or value.line < 1:
            errors.append("anchor:line:invalid")
        if type(value.column) is not int or value.column < 0:
            errors.append("anchor:column:invalid")
    elif isinstance(value, ActionRequest):
        if value.schema != ACTION_REQUEST_SCHEMA:
            errors.append("request:schema:unsupported")
        if not _nonempty_text(value.action_id):
            errors.append("request:action_id:invalid")
        if not isinstance(value.kind, ActionKind):
            errors.append("request:kind:invalid")
        try:
            arguments = json.loads(value.arguments_json)
            if not isinstance(arguments, dict):
                errors.append("request:arguments:not_object")
            elif value.arguments_json != _canonical_json_value(arguments):
                errors.append("request:arguments:not_canonical")
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append("request:arguments:invalid_json")
        errors.extend(validate(value.repository_snapshot))
        if value.repository_snapshot_sha256 != canonical_sha256(value.repository_snapshot):
            errors.append("request:snapshot_sha256:mismatch")
        if value.configuration_sha256 != canonical_sha256(
            value.repository_snapshot.configuration
        ):
            errors.append("request:configuration_sha256:mismatch")
        if not isinstance(value.requested_fidelity, RequestedFidelity):
            errors.append("request:requested_fidelity:invalid")
        if not isinstance(value.original_shell_form, str) or "\x00" in value.original_shell_form:
            errors.append("request:original_shell_form:invalid")
    elif isinstance(value, EvidenceArtifact):
        if value.schema != EVIDENCE_ARTIFACT_SCHEMA:
            errors.append("artifact:schema:unsupported")
        for field in (
            "request_sha256", "envelope_sha256", "snapshot_sha256",
            "configuration_sha256", "raw_fallback_sha256",
        ):
            if not _is_sha256(getattr(value, field)):
                errors.append(f"artifact:{field}:invalid")
        if not _nonempty_text(value.action_id):
            errors.append("artifact:action_id:invalid")
        for field in ("producer", "producer_version"):
            if not _nonempty_text(getattr(value, field)):
                errors.append(f"artifact:{field}:invalid")
        try:
            decoded = json.loads(value.direct_answer_json)
            if value.direct_answer_json != _canonical_json_value(decoded):
                errors.append("artifact:direct_answer:not_canonical")
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append("artifact:direct_answer:invalid_json")
        for anchor in value.anchors:
            errors.extend(validate(anchor))
        if any(not _nonempty_text(item) for item in value.witnesses):
            errors.append("artifact:witnesses:invalid")
        if not isinstance(value.semantics, EvidenceSemantics):
            errors.append("artifact:semantics:invalid")
        if not isinstance(value.coverage, Coverage):
            errors.append("artifact:coverage:invalid")
        if value.artifact_id != _artifact_identity(value):
            errors.append("artifact:artifact_id:mismatch")
    elif isinstance(value, InterceptionDecision):
        if value.schema != INTERCEPTION_DECISION_SCHEMA:
            errors.append("decision:schema:unsupported")
        if not _nonempty_text(value.action_id):
            errors.append("decision:action_id:invalid")
        if not _is_sha256(value.action_request_sha256):
            errors.append("decision:action_request_sha256:invalid")
        if not isinstance(value.mode, InterceptionMode):
            errors.append("decision:mode:invalid")
        if any(not _is_sha256(item) for item in value.artifact_ids):
            errors.append("decision:artifact_ids:invalid")
        if not value.reason_codes or any(not _nonempty_text(item) for item in value.reason_codes):
            errors.append("decision:reason_codes:invalid")
        if value.mode is InterceptionMode.REPLACE and value.raw_result_required:
            errors.append("decision:replace:raw_result_required")
        if value.mode is not InterceptionMode.REPLACE and not value.raw_result_required:
            errors.append("decision:fallback:raw_result_missing")
    elif isinstance(value, DeliveryReceipt):
        if value.schema != DELIVERY_RECEIPT_SCHEMA:
            errors.append("receipt:schema:unsupported")
        for field in (
            "action_request_sha256", "repository_snapshot_sha256",
            "interception_decision_sha256", "raw_result_sha256",
            "transformation_inputs_sha256", "final_observation_sha256",
            "provider_payload_sha256",
        ):
            if not _is_sha256(getattr(value, field)):
                errors.append(f"receipt:{field}:invalid")
        if not _is_sha256(value.provider_response_sha256, allow_empty=True):
            errors.append("receipt:provider_response_sha256:invalid")
        if not _is_sha256(value.immediate_next_action_sha256, allow_empty=True):
            errors.append("receipt:immediate_next_action_sha256:invalid")
        for field in (
            "transformation_version", "model_call_id", "observation_id",
            "provider_response_id",
        ):
            if not _nonempty_text(getattr(value, field)):
                errors.append(f"receipt:{field}:invalid")
        if value.delivery_state != DeliveryState.RESPONSE_COMMITTED.value:
            errors.append("receipt:delivery_state:not_response_committed")
        if not value.provider_response_sha256:
            errors.append("receipt:provider_response_sha256:required")
    else:
        return ("contract:wrong_type",)
    return tuple(errors)


__all__ = [
    "ACTION_REQUEST_SCHEMA",
    "CONFIGURATION_BINDING_SCHEMA",
    "DELIVERY_RECEIPT_SCHEMA",
    "EVIDENCE_ARTIFACT_SCHEMA",
    "INTERCEPTION_DECISION_SCHEMA",
    "REPOSITORY_SNAPSHOT_SCHEMA",
    "ActionKind",
    "ActionRequest",
    "ConfigurationBinding",
    "Coverage",
    "DeliveryReceipt",
    "EvidenceArtifact",
    "EvidenceSemantics",
    "InterceptionDecision",
    "InterceptionMode",
    "RequestedFidelity",
    "RepositorySnapshot",
    "SourceAnchor",
    "artifact_from_envelope",
    "canonical_bytes",
    "canonical_sha256",
    "evaluate_interception",
    "receipt_from_delivery_attempt",
    "validate",
]
