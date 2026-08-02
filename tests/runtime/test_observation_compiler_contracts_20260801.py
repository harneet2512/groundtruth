from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from groundtruth.runtime.evidence_envelope import EvidenceEnvelope, VERIFIED
from groundtruth.runtime.observation_compiler import (
    ACTION_REQUEST_SCHEMA,
    CONFIGURATION_BINDING_SCHEMA,
    DELIVERY_RECEIPT_SCHEMA,
    EVIDENCE_ARTIFACT_SCHEMA,
    INTERCEPTION_DECISION_SCHEMA,
    REPOSITORY_SNAPSHOT_SCHEMA,
    ActionKind,
    ActionRequest,
    ConfigurationBinding,
    Coverage,
    DeliveryReceipt,
    EvidenceArtifact,
    EvidenceSemantics,
    InterceptionMode,
    RequestedFidelity,
    RepositorySnapshot,
    SourceAnchor,
    artifact_from_envelope,
    canonical_bytes,
    canonical_sha256,
    evaluate_interception,
    receipt_from_delivery_attempt,
    validate,
)
from groundtruth.runtime.reasoning_runtime import (
    DeliveryAttempt,
    DeliveryState,
    ProviderTerminalKind,
    RevisionVector,
)


def _sha(char: str) -> str:
    return char * 64


def _configuration() -> ConfigurationBinding:
    return ConfigurationBinding(
        schema=CONFIGURATION_BINDING_SCHEMA,
        configuration_id="python-3.12-linux",
        inputs_sha256=_sha("1"),
        language_manifest_sha256=_sha("2"),
        build_system="pytest",
    )


def _snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        schema=REPOSITORY_SNAPSHOT_SCHEMA,
        repository_id="example/repo",
        root_sha256=_sha("3"),
        git_revision="abc123",
        dirty_diff_sha256=_sha("4"),
        working_tree_sha256=_sha("5"),
        revisions=RevisionVector(
            repository_content="repo-r7",
            graph="graph-r7",
            lsp="lsp-r7",
            runtime_evidence="runtime-r7",
        ),
        configuration=_configuration(),
    )


def _request(kind: ActionKind = ActionKind.EXACT_LITERAL_SEARCH) -> ActionRequest:
    arguments = (
        {"literal": "Widget", "paths": ["src"]}
        if kind is ActionKind.EXACT_LITERAL_SEARCH
        else {"path": "src/widget.py"}
        if kind is ActionKind.SYNTAX_QUERY
        else {}
    )
    return ActionRequest.build(
        action_id="action-7",
        kind=kind,
        arguments=arguments,
        snapshot=_snapshot(),
        requested_fidelity=RequestedFidelity.EXACT,
        original_shell_form="",
    )


def _envelope() -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="literal_search",
        fact_id="Widget",
        target="src/widget.py::Widget",
        evidence_type="exact_literal_search",
        payload=("src/widget.py:10",),
        provenance=(("src/widget.py", 10),),
        confidence=1.0,
        tier=VERIFIED,
        graph_revision="repo-r7",
    )


def _artifact() -> EvidenceArtifact:
    return artifact_from_envelope(
        request=_request(),
        envelope=_envelope(),
        producer_version="gt-index/v15.2",
        semantics=EvidenceSemantics.EXACT,
        direct_answer={"matches": [{"file": "src/widget.py", "line": 10}]},
        coverage=Coverage.COMPLETE,
        witnesses=("node:42",),
        raw_fallback=b"src/widget.py:10\n",
    )


def test_contracts_are_frozen_canonical_and_bind_configuration_and_snapshot() -> None:
    request = _request()
    artifact = _artifact()
    decision = evaluate_interception(request, (artifact,))

    assert request.schema == ACTION_REQUEST_SCHEMA
    assert artifact.schema == EVIDENCE_ARTIFACT_SCHEMA
    assert decision.schema == INTERCEPTION_DECISION_SCHEMA
    assert decision.mode is InterceptionMode.REPLACE
    assert decision.raw_result_required is False
    assert validate(request) == ()
    assert validate(artifact) == ()
    assert validate(decision) == ()
    assert canonical_bytes(request) == canonical_bytes(request)

    other_config = replace(_configuration(), inputs_sha256=_sha("9"))
    other_snapshot = replace(_snapshot(), configuration=other_config)
    rebound = ActionRequest.build(
        action_id=request.action_id,
        kind=request.kind,
        arguments={"paths": ["src"], "literal": "Widget"},
        snapshot=other_snapshot,
        requested_fidelity=request.requested_fidelity,
    )
    assert canonical_sha256(rebound) != canonical_sha256(request)


def test_argument_key_order_is_not_request_identity() -> None:
    left = ActionRequest.build(
        action_id="a",
        kind=ActionKind.EXACT_LITERAL_SEARCH,
        arguments={"literal": "needle", "paths": ["src", "lib"]},
        snapshot=_snapshot(),
        requested_fidelity=RequestedFidelity.EXACT,
    )
    right = ActionRequest.build(
        action_id="a",
        kind=ActionKind.EXACT_LITERAL_SEARCH,
        arguments={"paths": ["src", "lib"], "literal": "needle"},
        snapshot=_snapshot(),
        requested_fidelity=RequestedFidelity.EXACT,
    )

    assert left.arguments_json == right.arguments_json
    assert canonical_sha256(left) == canonical_sha256(right)


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        (
            lambda artifact: replace(artifact, coverage=Coverage.PARTIAL),
            "COVERAGE_NOT_COMPLETE",
        ),
        (
            lambda artifact: replace(artifact, ambiguity=("two definitions",)),
            "AMBIGUOUS_EVIDENCE",
        ),
        (
            lambda artifact: replace(artifact, omissions=("generated files",)),
            "EVIDENCE_HAS_OMISSIONS",
        ),
        (
            lambda artifact: replace(
                artifact, semantics=EvidenceSemantics.SOUND_OVERAPPROX
            ),
            "SEMANTICS_NOT_EXACT",
        ),
        (
            lambda artifact: replace(artifact, snapshot_sha256=_sha("8")),
            "STALE_SNAPSHOT",
        ),
    ],
)
def test_replacement_fails_closed_to_augmentation(changed, reason: str) -> None:
    decision = evaluate_interception(_request(), (changed(_artifact()),))

    assert decision.mode is InterceptionMode.AUGMENT
    assert decision.raw_result_required is True
    assert reason in decision.reason_codes


def test_literal_shell_semantics_are_never_reinterpreted() -> None:
    shell = _request(ActionKind.SHELL)
    artifact = replace(_artifact(), action_id=shell.action_id)

    decision = evaluate_interception(shell, (artifact,))

    assert decision.mode is InterceptionMode.PASS_THROUGH
    assert decision.raw_result_required is True
    assert decision.reason_codes == ("UNTYPED_ACTION",)


def test_execution_specific_evidence_keeps_raw_diagnostics() -> None:
    request = _request(ActionKind.RUN_VERIFICATION)
    artifact = replace(
        _artifact(),
        action_id=request.action_id,
        request_sha256=canonical_sha256(request),
        semantics=EvidenceSemantics.EXECUTION_SPECIFIC,
    )

    decision = evaluate_interception(request, (artifact,))

    assert decision.mode is InterceptionMode.AUGMENT
    assert decision.raw_result_required is True
    assert "RAW_DIAGNOSTICS_REQUIRED" in decision.reason_codes


def test_patch_impact_and_explicit_raw_fidelity_are_augmentation_only() -> None:
    patch_request = _request(ActionKind.PATCH_IMPACT)
    patch_artifact = artifact_from_envelope(
        request=patch_request,
        envelope=_envelope(),
        producer_version="gt-index/v15.2",
        semantics=EvidenceSemantics.EXACT,
        direct_answer={"changed_symbols": ["Widget"]},
        coverage=Coverage.COMPLETE,
        raw_fallback=b"diff --git a/src/widget.py b/src/widget.py",
    )
    patch_decision = evaluate_interception(patch_request, (patch_artifact,))
    assert patch_decision.mode is InterceptionMode.AUGMENT
    assert "ACTION_NOT_REPLACEABLE" in patch_decision.reason_codes

    raw_request = ActionRequest.build(
        action_id="raw-action",
        kind=ActionKind.EXACT_LITERAL_SEARCH,
        arguments={"literal": "Widget", "paths": ["src"]},
        snapshot=_snapshot(),
        requested_fidelity=RequestedFidelity.RAW,
    )
    # Rebuilding the artifact through its authority-preserving adapter also
    # gives it the correct content identity for this request.
    raw_artifact = artifact_from_envelope(
        request=raw_request,
        envelope=_envelope(),
        producer_version="gt-index/v15.2",
        semantics=EvidenceSemantics.EXACT,
        direct_answer={"matches": []},
        coverage=Coverage.COMPLETE,
        raw_fallback=b"raw",
    )
    raw_decision = evaluate_interception(raw_request, (raw_artifact,))
    assert raw_decision.mode is InterceptionMode.AUGMENT
    assert "REQUEST_REQUIRES_RAW" in raw_decision.reason_codes


def test_envelope_translation_preserves_producer_authority_and_freshness() -> None:
    artifact = _artifact()

    assert artifact.producer == _envelope().producer
    assert artifact.envelope_sha256 == hashlib.sha256(
        canonical_bytes(_envelope())
    ).hexdigest()
    assert artifact.anchors == (SourceAnchor("src/widget.py", 10, 0),)
    assert artifact.snapshot_sha256 == canonical_sha256(_snapshot())
    assert artifact.configuration_sha256 == canonical_sha256(_configuration())

    stale = artifact_from_envelope(
        request=_request(),
        envelope=replace(_envelope(), valid_until="repo-r6"),
        producer_version="gt-index/v15.2",
        semantics=EvidenceSemantics.EXACT,
        direct_answer={"definitions": []},
        coverage=Coverage.COMPLETE,
        raw_fallback=b"",
    )
    decision = evaluate_interception(_request(), (stale,))
    assert decision.mode is InterceptionMode.AUGMENT
    assert "PRODUCER_REVISION_STALE" in decision.reason_codes


def test_delivery_receipt_is_a_projection_not_a_second_delivery_authority() -> None:
    request = _request()
    artifact = _artifact()
    decision = evaluate_interception(request, (artifact,))
    delivery = DeliveryAttempt(
        evidence_ids=(artifact.artifact_id,),
        capsule_hash=_sha("a"),
        model_call_id="call-1",
        state=DeliveryState.RESPONSE_COMMITTED,
        observation_id="observation-1",
        joined_capsule_hash=_sha("a"),
        provider_payload_hash=_sha("b"),
        provider_response_id="response-1",
        terminal_kind=ProviderTerminalKind.COMPLETED,
        response_hash=_sha("c"),
    )

    receipt = receipt_from_delivery_attempt(
        request=request,
        decision=decision,
        attempt=delivery,
        raw_result=b"raw output",
        final_observation=b"compiled output",
        transformation_version="observation-compiler/v1",
        transformation_inputs=(artifact.artifact_id,),
        immediate_next_action_sha256=_sha("d"),
    )

    assert receipt.schema == DELIVERY_RECEIPT_SCHEMA
    assert receipt.delivery_state == DeliveryState.RESPONSE_COMMITTED.value
    assert receipt.provider_payload_sha256 == delivery.provider_payload_hash
    assert receipt.provider_response_id == delivery.provider_response_id
    assert receipt.provider_response_sha256 == delivery.response_hash
    assert validate(receipt) == ()
    assert "receipt:delivery_state:not_response_committed" in validate(
        replace(receipt, delivery_state=DeliveryState.DELIVERED.value)
    )
    assert canonical_sha256(receipt) != canonical_sha256(
        replace(receipt, immediate_next_action_sha256=_sha("e"))
    )


def test_receipt_rejects_unbound_or_pre_delivery_attempts() -> None:
    attempt = DeliveryAttempt(
        evidence_ids=("artifact",),
        capsule_hash=_sha("a"),
        model_call_id="call-1",
    )
    with pytest.raises(ValueError, match="response-committed delivery attempt"):
        receipt_from_delivery_attempt(
            request=_request(),
            decision=evaluate_interception(_request(), (_artifact(),)),
            attempt=attempt,
            raw_result=b"raw",
            final_observation=b"final",
            transformation_version="v1",
            transformation_inputs=(),
            immediate_next_action_sha256="",
        )

    with pytest.raises(ValueError, match="DELIVERED requires provider response proof"):
        DeliveryAttempt(
            evidence_ids=("artifact",),
            capsule_hash=_sha("a"),
            model_call_id="call-1",
            state=DeliveryState.DELIVERED,
            observation_id="observation-1",
            joined_capsule_hash=_sha("a"),
            provider_payload_hash=_sha("b"),
        )


def test_freshness_domain_is_selected_by_action_kind() -> None:
    literal = _artifact()
    assert evaluate_interception(_request(), (literal,)).mode is InterceptionMode.REPLACE

    graph_revision_artifact = artifact_from_envelope(
        request=_request(),
        envelope=replace(_envelope(), valid_until="graph-r7"),
        producer_version="v1",
        semantics=EvidenceSemantics.EXACT,
        direct_answer={"matches": []},
        coverage=Coverage.COMPLETE,
        raw_fallback=b"",
    )
    decision = evaluate_interception(_request(), (graph_revision_artifact,))
    assert decision.mode is InterceptionMode.AUGMENT
    assert "PRODUCER_REVISION_STALE" in decision.reason_codes

    verification_request = _request(ActionKind.VERIFICATION_STATUS)
    runtime_artifact = artifact_from_envelope(
        request=verification_request,
        envelope=replace(_envelope(), valid_until="runtime-r7"),
        producer_version="v1",
        semantics=EvidenceSemantics.EXECUTION_SPECIFIC,
        direct_answer={"green": True},
        coverage=Coverage.COMPLETE,
        raw_fallback=b"verification diagnostics",
    )
    runtime_decision = evaluate_interception(
        verification_request,
        (runtime_artifact,),
    )
    assert runtime_decision.mode is InterceptionMode.AUGMENT
    assert "PRODUCER_REVISION_STALE" not in runtime_decision.reason_codes
    assert "RAW_DIAGNOSTICS_REQUIRED" in runtime_decision.reason_codes


def test_removed_symbol_operations_are_not_public_action_kinds() -> None:
    values = {kind.value for kind in ActionKind}
    assert values.isdisjoint({"find_definition", "find_references", "find_callers"})
    for removed in ("find_definition", "find_references", "find_callers"):
        with pytest.raises(ValueError):
            ActionKind(removed)


def test_direct_construction_cannot_claim_a_mismatched_artifact_or_decision() -> None:
    artifact = replace(_artifact(), action_id="other-action")
    decision = evaluate_interception(_request(), (artifact,))
    assert decision.mode is InterceptionMode.AUGMENT
    assert "artifact:action_id:mismatch" in decision.reason_codes

    invalid_receipt = DeliveryReceipt(
        schema=DELIVERY_RECEIPT_SCHEMA,
        action_request_sha256=_sha("1"),
        repository_snapshot_sha256=_sha("2"),
        interception_decision_sha256=_sha("3"),
        raw_result_sha256=_sha("4"),
        transformation_version="v1",
        transformation_inputs_sha256=_sha("5"),
        final_observation_sha256=_sha("6"),
        delivery_state=DeliveryState.RESPONSE_COMMITTED.value,
        model_call_id="call",
        observation_id="obs",
        provider_payload_sha256="",
        provider_response_id="response",
        provider_response_sha256=_sha("7"),
        immediate_next_action_sha256="",
    )
    assert "receipt:provider_payload_sha256:invalid" in validate(invalid_receipt)
