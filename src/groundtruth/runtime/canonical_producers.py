"""Pure producer bridges for canonical decision evidence.

These bridges consume structured results owned by the existing deterministic
engines and emit :class:`EvidenceEnvelope` objects with producer-owned canonical
semantics.  They do not parse transcripts, schedule evidence, select coalitions,
render capsules, or mutate model observations.

Correct-or-quiet is deliberate: an incomplete result shape, an unproved causal
attribution, invalid source provenance, or crossed completion state returns
``None``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from typing import Any, Mapping

# Fully-qualified module imports, matching every other shipped runtime module. The
# `from groundtruth.runtime import X` form records a dependency on the PACKAGE, which is
# not a shipped module, so the injection import-closure check reports a hole and the
# in-container import can fail. See tests/test_deepswe_injection_import_coverage.py.
import groundtruth.runtime.evidence_envelope as ee
import groundtruth.runtime.fact_registry as fact_registry
from groundtruth.runtime.covering_runner import CoveringAttribution
from groundtruth.runtime.feature_lineage import (
    CAP_BYTE_OWNER_MECHANISMS,
    DeliveryLineage,
    FeatureRef,
    build_lineage,
)
from groundtruth.runtime.hypothesis_ledger import (
    Advisory,
    D_ALTERNATE_SURFACE_CANDIDATE,
    D_HYPOTHESIS_FALSIFIED,
    D_REPAIR_NOT_SOURCE,
    D_REQUEST_NEW_HYPOTHESIS,
    D_REFRESH_BEFORE_ADVICE,
    advisory_violations,
)
from groundtruth.runtime.reasoning_runtime import (
    Authority,
    CanonicalEvidenceSemantics,
    MandatoryReason,
    RevisionVector,
    feature_contract_for,
)
from groundtruth.runtime.submit_gate import CompletionCertificate, GateVerdict

__all__ = [
    "CoveringRedComputation",
    "ProducerContext",
    "RecoveryComputation",
    "SubmitEvidenceOwner",
    "SubmitRefusalComputation",
    "SyntaxResultComputation",
    "produce_covering_red",
    "produce_recovery",
    "produce_submit_refusal",
    "produce_syntax_result",
]


_ACTIONABLE_RECOVERY_DISPOSITIONS = frozenset(
    {
        D_ALTERNATE_SURFACE_CANDIDATE,
        D_HYPOTHESIS_FALSIFIED,
        D_REPAIR_NOT_SOURCE,
        D_REQUEST_NEW_HYPOTHESIS,
        D_REFRESH_BEFORE_ADVICE,
    }
)
_ATTRIBUTION_METHODS = frozenset({"trace_frame", "differential", "unresolved_covering"})
_FINE_TO_COARSE_EVENT = {
    fact_registry.EVENT_EDIT_RESULT: ee.EVENT_EDIT,
    fact_registry.EVENT_FAILURE_OBS: ee.EVENT_TEST,
    fact_registry.EVENT_SUBMIT: ee.EVENT_SUBMIT,
}


@dataclass(frozen=True)
class ProducerContext:
    """Repository-bound subject and exact proof shared by a producer call.

    Repository facts carry source provenance. Runtime-derived facts may instead
    carry exact canonical event/computation/diagnostic witnesses; manufacturing a
    source line for those facts is forbidden.
    """

    subject: str
    provenance: tuple[tuple[str, int], ...]
    revision: RevisionVector
    decision_id: str
    causal_neighborhood: tuple[str, ...]
    runtime_witnesses: tuple[ee.CanonicalRuntimeWitness, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provenance",
            tuple((str(path), int(line)) for path, line in self.provenance),
        )
        object.__setattr__(self, "causal_neighborhood", tuple(self.causal_neighborhood))
        object.__setattr__(self, "runtime_witnesses", tuple(self.runtime_witnesses))
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("producer subject is required")
        if not isinstance(self.revision, RevisionVector):
            raise TypeError("producer revision must be a RevisionVector")
        if not isinstance(self.decision_id, str) or not self.decision_id.strip():
            raise ValueError("producer decision_id is required")
        if any(not path or line < 1 for path, line in self.provenance):
            raise ValueError("source provenance rows require a path and positive line")
        witness_violations = [
            violation
            for witness in self.runtime_witnesses
            for violation in ee.runtime_witness_violations(witness)
        ]
        if witness_violations:
            raise ValueError("invalid canonical runtime witness: " + "; ".join(witness_violations))
        subject_path = _subject_path(self.subject)
        if any(
            witness.source_path and _normalized_path(witness.source_path) != subject_path
            for witness in self.runtime_witnesses
        ):
            raise ValueError("diagnostic runtime witness must locate the producer subject")
        if not self.provenance and not self.runtime_witnesses:
            raise ValueError("source provenance or canonical runtime witness is required")
        if not self.causal_neighborhood:
            raise ValueError("producer causal neighborhood is required")
        if any(not isinstance(item, str) or not item.strip() for item in self.causal_neighborhood):
            raise ValueError("producer causal neighborhood entries must be strings")


@dataclass(frozen=True)
class CoveringRedComputation:
    executed: bool
    verdict: str
    reason: str
    command: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    attribution_method: str
    attribution_base_verdict: str
    implicated_edited_paths: tuple[str, ...]
    covering_file_count: int
    result_json: str
    attribution_json: str


@dataclass(frozen=True)
class SyntaxResultComputation:
    verdict: str
    reason: str
    language: str
    checker: tuple[str, ...]
    diagnostic: str
    diagnostic_sha256: str
    result_json: str


@dataclass(frozen=True)
class RecoveryComputation:
    transition: str
    disposition: str
    tier: str
    statement: str
    evidence_ids: tuple[str, ...]


class SubmitEvidenceOwner(str, Enum):
    """The physical submit output whose bytes the canonical fact will represent."""

    REFUSAL = "GT_SS_SUBMIT_RED"
    CERTIFICATE = "GT_CERT_DELIVERY"


@dataclass(frozen=True)
class SubmitRefusalComputation:
    reason: str
    detail: str
    gate_record_json: str
    output_owner: SubmitEvidenceOwner
    certificate_json: str
    patch_revision: str


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_data(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _stable_data(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _stable_data(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_stable_data(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical producer value: {type(value).__name__}")


def _stable_json(value: Any) -> str:
    return json.dumps(
        _stable_data(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stable_json_or_none(value: Any) -> str | None:
    try:
        return _stable_json(value)
    except (TypeError, ValueError):
        return None


def _normalized_path(value: str) -> str:
    return (value or "").replace("\\", "/").lstrip("./")


def _subject_path(subject: str) -> str:
    return _normalized_path(subject.split("::", 1)[0])


def _context_has_exact_proof(context: ProducerContext) -> bool:
    subject_path = _subject_path(context.subject)
    source_provenanced = bool(subject_path) and subject_path in {
        _normalized_path(path) for path, _line in context.provenance
    }
    # A runtime witness is an alternative to source provenance, never a way to
    # excuse crossed source rows that would still be rendered to the model.
    if context.provenance:
        return source_provenanced
    return bool(context.runtime_witnesses)


def _lineage(
    *,
    producer: str,
    evidence_type: str,
    owner_feature_id: str | None,
) -> DeliveryLineage | None:
    actual_event = fact_registry.required_event(evidence_type)
    if actual_event is None:
        return None
    lineage = build_lineage(
        runtime_producer_id=producer,
        evidence_type=evidence_type,
        actual_event=actual_event,
    )
    if lineage is None or not lineage.producer_registration_match:
        return None
    if owner_feature_id is None:
        return lineage
    mechanism = CAP_BYTE_OWNER_MECHANISMS.get(owner_feature_id)
    if mechanism is None or not any(
        binding.fact_class == lineage.fact_class for binding in mechanism.bindings
    ):
        return None
    features = set(lineage.features)
    features.add(FeatureRef("CAP", owner_feature_id, "byte_owner"))
    return replace(lineage, features=tuple(sorted(features)))


def _build_envelope(
    *,
    context: ProducerContext,
    producer: str,
    evidence_type: str,
    payload: tuple[str, ...],
    claim: str,
    consequence: str,
    tier: str,
    confidence: float,
    computation: object,
    mandatory_reason: MandatoryReason | None,
    owner_feature_id: str | None,
    failure_prevention: int,
    causal_value: int,
    contradiction_resolution: int,
    anchoring_risk: int,
    observed_substrates: tuple[str, ...],
    identity: object | None = None,
) -> ee.EvidenceEnvelope | None:
    if not _context_has_exact_proof(context):
        return None
    registration = fact_registry.registration_for(evidence_type)
    lineage = _lineage(
        producer=producer,
        evidence_type=evidence_type,
        owner_feature_id=owner_feature_id,
    )
    if registration is None or lineage is None:
        return None
    contract = feature_contract_for(lineage.fact_class)
    if contract is None:
        return None
    preferred_event = _FINE_TO_COARSE_EVENT.get(lineage.actual_event)
    if preferred_event is None:
        return None
    semantics = CanonicalEvidenceSemantics(
        decision_context=contract.decision_context,
        roles=contract.roles,
        claim=claim,
        actionable_consequence=consequence,
        causal_neighborhood=tuple(
            dict.fromkeys(
                (
                    f"subject:{context.subject}",
                    f"decision:{context.decision_id}",
                    f"decision:{contract.decision_context.value}",
                    *context.causal_neighborhood,
                )
            )
        ),
        authority=Authority.RESULT_DERIVED,
        revision=context.revision,
        revision_dependencies=contract.revision_dependencies,
        mandatory_reason=mandatory_reason,
        failure_prevention=failure_prevention,
        causal_value=causal_value,
        contradiction_resolution=contradiction_resolution,
        anchoring_risk=anchoring_risk,
        observed_substrates=tuple(sorted(set(observed_substrates))),
    )
    try:
        computation_hash = _sha256_text(_stable_json(computation if identity is None else identity))
        for witness in context.runtime_witnesses:
            if (
                witness.kind == "deterministic_computation"
                and witness.content_sha256 != computation_hash
            ):
                return None
            if witness.kind == "diagnostic_location":
                diagnostic_hash = getattr(computation, "diagnostic_sha256", None)
                if witness.content_sha256 != diagnostic_hash:
                    return None
        envelope = ee.EvidenceEnvelope.build(
            producer=producer,
            fact_id=f"{lineage.fact_class}:{computation_hash[:20]}",
            target=context.subject,
            evidence_type=evidence_type,
            payload=payload,
            provenance=context.provenance,
            confidence=confidence,
            tier=tier,
            graph_revision=context.revision.graph,
            valid_until=context.revision.graph,
            preferred_event=preferred_event,
            blocking_eligibility=ee.ADVISORY,
            estimated_cost_tokens=max(
                1,
                (len(claim) + len(consequence) + sum(len(item) for item in payload)) // 4,
            ),
            measured=tier == ee.VERIFIED,
            lineage=lineage,
            producer_inputs=computation,
            canonical_semantics=semantics,
            runtime_witnesses=context.runtime_witnesses,
        )
    except (TypeError, ValueError):
        return None
    if ee.validate(envelope):
        return None
    return envelope


def produce_covering_red(
    *,
    context: ProducerContext,
    result: Mapping[str, Any],
    attribution: CoveringAttribution,
) -> ee.EvidenceEnvelope | None:
    """Emit an attributable executed covering RED, otherwise abstain."""

    if not isinstance(result, Mapping) or not isinstance(attribution, CoveringAttribution):
        return None
    command = result.get("command")
    exit_code = result.get("exit_code")
    selected_files = result.get("files")
    ran_files = result.get("ran")
    if (
        result.get("executed") is not True
        or result.get("verdict") != "fail"
        or type(exit_code) is not int
        or exit_code == 0
        or not isinstance(command, (tuple, list))
        or not command
        or any(not isinstance(item, str) or not item for item in command)
        or not isinstance(selected_files, (tuple, list))
        or not selected_files
        or any(not isinstance(item, str) or not item for item in selected_files)
        or not isinstance(ran_files, (tuple, list))
        or not ran_files
        or any(not isinstance(item, str) or not item for item in ran_files)
        or not attribution.attributed
        or attribution.current_verdict != "fail"
        or attribution.method not in _ATTRIBUTION_METHODS
        or not attribution.implicated_edited_paths
        or not attribution.covering_files
    ):
        return None
    subject_path = _subject_path(context.subject)
    selected = {_normalized_path(path) for path in selected_files}
    ran = {_normalized_path(path) for path in ran_files}
    attributed_covering = {_normalized_path(path) for path in attribution.covering_files}
    implicated = {_normalized_path(path) for path in attribution.implicated_edited_paths}
    if (
        subject_path not in implicated
        or not attributed_covering.issubset(selected)
        or not attributed_covering.intersection(ran)
    ):
        return None
    stdout = result.get("stdout_tail")
    stderr = result.get("stderr_tail")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    result_json = _stable_json_or_none(result)
    attribution_json = _stable_json_or_none(attribution)
    if result_json is None or attribution_json is None:
        return None
    computation = CoveringRedComputation(
        executed=True,
        verdict="fail",
        reason=str(result.get("reason") or ""),
        command=tuple(command),
        exit_code=exit_code,
        stdout_sha256=_sha256_text(stdout),
        stderr_sha256=_sha256_text(stderr),
        attribution_method=str(attribution.method),
        attribution_base_verdict=attribution.base_verdict,
        implicated_edited_paths=tuple(attribution.implicated_edited_paths),
        covering_file_count=len(attribution.covering_files),
        result_json=result_json,
        attribution_json=attribution_json,
    )
    return _build_envelope(
        context=context,
        producer="covering_runner",
        evidence_type="covering_red",
        payload=(
            "verdict=fail",
            f"attribution={attribution.method}",
            f"source={subject_path}",
        ),
        claim=(
            f"The executed repository covering check for {context.subject} is "
            "red and attributable to the current patch."
        ),
        consequence=(
            "The open failure-recovery decision remains constrained by this observed failing path."
        ),
        tier=ee.VERIFIED,
        confidence=0.98,
        computation=computation,
        mandatory_reason=MandatoryReason.BLOCKER,
        owner_feature_id=None,
        failure_prevention=10,
        causal_value=9,
        contradiction_resolution=8,
        anchoring_risk=0,
        observed_substrates=("structured_test_result",),
    )


def produce_syntax_result(
    *,
    context: ProducerContext,
    result: Mapping[str, Any],
) -> ee.EvidenceEnvelope | None:
    """Emit only positive parser/undefined-name evidence from ``edit_check``."""

    if not isinstance(result, Mapping):
        return None
    verdict = result.get("verdict")
    reason = result.get("reason")
    diagnostic = result.get("diagnostic")
    checker = result.get("checker")
    allowed = {
        "syntax_error": "parse_error",
        "name_error": "undefined_name",
    }
    if (
        verdict not in allowed
        or reason != allowed[verdict]
        or not isinstance(diagnostic, str)
        or not diagnostic.strip()
        or "<gt-" in diagnostic.lower()
        or not isinstance(checker, (tuple, list))
        or not checker
        or any(not isinstance(item, str) or not item for item in checker)
    ):
        return None
    reason_s = str(reason)
    result_json = _stable_json_or_none(result)
    if result_json is None:
        return None
    computation = SyntaxResultComputation(
        verdict=verdict,
        reason=reason_s,
        language=str(result.get("language") or ""),
        checker=tuple(checker),
        diagnostic=diagnostic,
        diagnostic_sha256=_sha256_text(diagnostic),
        result_json=result_json,
    )
    if verdict == "syntax_error":
        claim = f"{context.subject} has a parser-confirmed syntax error."
    else:
        claim = f"{context.subject} has a definite undefined-name error."
    return _build_envelope(
        context=context,
        producer="edit_check",
        evidence_type="syntax_result",
        payload=(diagnostic,),
        claim=claim,
        consequence=(
            "Patch propagation remains structurally invalid until this exact "
            "diagnostic no longer applies to the current edit."
        ),
        tier=ee.VERIFIED,
        confidence=0.99,
        computation=computation,
        mandatory_reason=MandatoryReason.BLOCKER,
        owner_feature_id="GT_EDIT_CHECK",
        failure_prevention=10,
        causal_value=10,
        contradiction_resolution=5,
        anchoring_risk=0,
        observed_substrates=(
            ("parser_result",) if verdict == "syntax_error" else ("compiler_result",)
        ),
    )


def produce_recovery(
    *,
    context: ProducerContext,
    advisory: Advisory,
) -> ee.EvidenceEnvelope | None:
    """Bridge one actionable, validated HypothesisLedger advisory."""

    if (
        not isinstance(advisory, Advisory)
        or advisory_violations(advisory)
        or advisory.disposition not in _ACTIONABLE_RECOVERY_DISPOSITIONS
        or not advisory.statement.strip()
        or not advisory.evidence_ids
        or "<gt-" in advisory.statement.lower()
    ):
        return None
    tier_to_confidence = {ee.WARNING: 0.78, ee.INFO: 0.58}
    confidence = tier_to_confidence.get(advisory.tier)
    if confidence is None:
        return None
    computation = RecoveryComputation(
        transition=advisory.transition,
        disposition=advisory.disposition,
        tier=advisory.tier,
        statement=advisory.statement,
        evidence_ids=tuple(advisory.evidence_ids),
    )
    return _build_envelope(
        context=context,
        producer="governor",
        evidence_type="recovery",
        payload=(advisory.statement,),
        claim=advisory.statement,
        consequence=(
            "The open failure-recovery decision has new discriminating evidence "
            "and should not silently repeat the contradicted trajectory."
        ),
        tier=advisory.tier,
        confidence=confidence,
        computation=computation,
        mandatory_reason=None,
        owner_feature_id="GT_HYPOTHESIS",
        failure_prevention=8,
        causal_value=7,
        contradiction_resolution=(9 if advisory.disposition == D_HYPOTHESIS_FALSIFIED else 6),
        anchoring_risk=2 if advisory.tier == ee.INFO else 1,
        observed_substrates=("canonical_event_history",),
    )


def produce_submit_refusal(
    *,
    context: ProducerContext,
    verdict: GateVerdict,
    output_owner: SubmitEvidenceOwner,
    certificate: CompletionCertificate | None = None,
) -> ee.EvidenceEnvelope | None:
    """Bridge a positive Gate Kernel refusal with its actual physical owner."""

    if (
        not isinstance(verdict, GateVerdict)
        or verdict.allow is not False
        or not isinstance(verdict.reason, str)
        or not verdict.reason.strip()
        or not isinstance(verdict.record, dict)
        or not verdict.record.get("block")
        or not isinstance(output_owner, SubmitEvidenceOwner)
    ):
        return None
    if certificate is not None and (
        not isinstance(certificate, CompletionCertificate)
        or certificate.decision != "bounce_once"
        or certificate.head_reason != verdict.reason
        or (
            certificate.patch_revision is not None
            and certificate.patch_revision != context.revision.repository_content
        )
    ):
        return None
    if output_owner is SubmitEvidenceOwner.CERTIFICATE and certificate is None:
        return None
    block = str(verdict.record.get("block") or "")
    if block == "covering_test_failed":
        public_reason = "covering_test_failed"
    elif block == "hygiene":
        public_reason = "hygiene"
    elif verdict.record.get("submit_blocking") is True:
        public_reason = "submit_observed_failure"
    else:
        return None
    certificate_json = (
        _stable_json_or_none(certificate.to_dict()) if certificate is not None else ""
    )
    gate_record_json = _stable_json_or_none(verdict.record)
    if certificate_json is None or gate_record_json is None:
        return None
    computation = SubmitRefusalComputation(
        reason=verdict.reason,
        detail=verdict.detail,
        gate_record_json=gate_record_json,
        output_owner=output_owner,
        certificate_json=certificate_json,
        patch_revision=(
            certificate.patch_revision
            if certificate is not None and certificate.patch_revision is not None
            else context.revision.repository_content
        ),
    )
    return _build_envelope(
        context=context,
        producer="submit_gate",
        evidence_type="submit_refusal",
        payload=(
            "completion_blocked",
            f"reason={public_reason}",
        ),
        claim=f"Completion is refused because {public_reason}.",
        consequence=(
            "The completion decision remains open while this verified blocking "
            "condition is present."
        ),
        tier=ee.VERIFIED,
        confidence=0.99,
        computation=computation,
        mandatory_reason=MandatoryReason.BLOCKER,
        owner_feature_id=output_owner.value,
        failure_prevention=10,
        causal_value=10,
        contradiction_resolution=7,
        anchoring_risk=0,
        observed_substrates=("canonical_validation_state",),
        identity={
            "gate_record": computation.gate_record_json,
            "patch_revision": computation.patch_revision,
            "reason": computation.reason,
        },
    )
