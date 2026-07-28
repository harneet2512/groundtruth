"""Canonical runtime proof attestation: exact delivery without inferred use."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent
from groundtruth.runtime.runtime_attestation import (
    AttestationIntegrityError,
    export_runtime_attestations,
    load_runtime_attestations,
    read_runtime_attestation_bundle,
    runtime_attestation_diagnostic,
)


REVISION = rr.RevisionVector(
    repository_content="repo-attestation",
    graph="graph-attestation",
    lsp="lsp-attestation",
    runtime_evidence="runtime-attestation",
)


def _runtime(tmp_path):
    path = tmp_path / "gt_reasoning_runtime.sqlite3"
    journal = rr.RuntimeJournal(path)
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-attestation",
        journal=journal,
        initial_revision=REVISION,
    )
    return runtime, journal, path


def _seed_reasoning_graph(runtime: rr.AttemptReasoningRuntime) -> None:
    action = rr.CanonicalAction(
        action_id="action-attestation",
        operation=rr.ActionOperation.SEARCH,
        tool_family="search",
        tool_name="mini-swe",
        structured_operation="search",
        subject="refreshSession",
        query="refreshSession",
        targets=("src",),
        raw_command="opaque structured search",
    )
    proposal = miniswe.canonicalize_action_proposal(
        action,
        event_id="event-attestation-proposal",
        attempt_id="attempt-attestation",
        sequence=1,
        model_turn_id="call-before-attestation",
        observation_id="obs-before-attestation",
        revision=REVISION,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="other",
            carrier_kind="other",
            command="carrier is audit-only",
            output="src/auth/session.py:41:def refreshSession(token):",
            exit_status=0,
            semantic_events=(),
            semantics_authoritative=True,
        ),
        proposal=proposal,
        result=rr.CanonicalResult(
            status="success",
            exit_code=0,
            hit_count=1,
            files_hit=("src/auth/session.py",),
        ),
        event_id="event-attestation-result",
        sequence=2,
        observation_id="obs-attestation-search",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )
    runtime.append_event(proposal)
    runtime.append_event(result)


def _evidence() -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id="GT-E-attestation",
        feature_id="caller_contract",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim="Callers require the Session return contract.",
        actionable_consequence="Preserve the Session return contract.",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            "decision:patch-attestation",
            "hyp:refreshSession",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.STRUCTURED,
        owner_feature_ids=(),
        # REQUIRED for release. The temporal gate authorizes a record only on substrates THAT
        # RECORD observed (`available_substrates ∩ evidence.observed_substrates`) -- an
        # attempt-wide union must never lend one record's assurance to another. A record
        # declaring nothing is held at PREREQUISITES_PENDING, so the coalition never forms,
        # nothing compiles, and `plan.delivery_attempt_id` comes back EMPTY -- which is how
        # these seven attestation tests were failing: not on anything they assert, but on a
        # fixture that no longer looks like a real producer.
        #
        # Production producers declare this (all 13 gateway `_mk_add` sites and every
        # canonical_producers builder), so taking it from the contract's own preferred
        # substrates is the faithful shape, not a workaround.
        observed_substrates=tuple(
            sorted(contract.fallback_policy.preferred_substrates)
        ),
    )


def _prepare(runtime: rr.AttemptReasoningRuntime) -> rr.InferencePlan:
    runtime.ingest_evidence(_evidence())
    _seed_reasoning_graph(runtime)
    return runtime.prepare_next_inference(
        decisions=(
            rr.ActiveDecision(
                decision_id="patch-attestation",
                context=rr.DecisionContext.PATCH_CONSTRUCTION,
                primary_claim="Safely patch refreshSession.",
                required_roles=(
                    rr.EvidenceRole.BEHAVIORAL_CONTRACT,
                    rr.EvidenceRole.AFFECTED_CALLER,
                ),
                causal_neighborhood=("hyp:refreshSession",),
                token_budget=180,
                current_revision=REVISION,
            ),
        ),
        satisfied_predicates=frozenset(
            {
                rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
                rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
                rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
                rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
                rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
                rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
            }
        ),
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=("graph", "lsp"),
        native_observation="$ sed -n '41,70p' src/auth/session.py\n",
        observation_id="obs-attestation",
        source_model_call_id="call-before-attestation",
        model_call_id="call-attestation",
    )


def _payload(plan: rr.InferencePlan) -> dict[str, object]:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": plan.compilation.capsule_text,
                    }
                ],
            }
        ],
        "api_key": "must-not-leave-the-journal-reader",
    }


def _complete(runtime: rr.AttemptReasoningRuntime, plan: rr.InferencePlan) -> None:
    payload = _payload(plan)
    runtime.bind_provider_payload(plan.delivery_attempt_id, payload)
    runtime.mark_dispatched(plan.delivery_attempt_id, payload)
    accepted = runtime.mark_provider_accepted(
        plan.delivery_attempt_id,
        provider_response_id="resp-sensitive-external-id",
    )
    runtime.record_provider_terminal(
        plan.delivery_attempt_id,
        rr.ModelCallAttempt(
            model_call_id=accepted.model_call_id,
            joined_capsule_hash=accepted.joined_capsule_hash,
            provider_payload_hash=accepted.provider_payload_hash,
            provider_response_id=accepted.provider_response_id,
            terminal_kind=rr.ProviderTerminalKind.COMPLETED,
        ),
    )
    runtime.commit_provider_response(
        plan.delivery_attempt_id,
        response_hash="f" * 64,
    )


def test_complete_chain_exports_hash_bound_feature_ownership_without_secrets(
    tmp_path,
) -> None:
    runtime, journal, path = _runtime(tmp_path)
    try:
        plan = _prepare(runtime)
        _complete(runtime, plan)

        loaded = load_runtime_attestations(
            path,
            attempt_id="attempt-attestation",
        )

        assert loaded.journal_present is True
        assert loaded.integrity_ok is True
        assert loaded.rejected == ()
        assert len(loaded.attestations) == 1
        proof = loaded.attestations[0]
        assert proof.lifecycle_states == (
            "SELECTED",
            "COMPILED",
            "JOINED",
            "DISPATCHED",
            "PROVIDER_ACCEPTED",
            "DELIVERED",
            "RESPONSE_COMMITTED",
        )
        assert proof.delivery_proven is True
        assert proof.response_committed is True
        assert proof.capsule_hash == plan.compilation.capsule_hash
        assert len(proof.provider_payload_hash) == 64
        assert proof.evidence[0].feature_id == "caller_contract"
        assert proof.evidence[0].owner_feature_ids == ()
        assert proof.explicit_acknowledgment == "UNKNOWN"
        assert proof.behavioral_influence == "NOT_EVALUATED"
        serialized = json.dumps(proof.as_dict(), sort_keys=True)
        assert "must-not-leave" not in serialized
        assert "resp-sensitive-external-id" not in serialized
        assert plan.compilation.capsule_text not in serialized
    finally:
        journal.close()


def test_joined_only_chain_is_progress_not_delivery(tmp_path) -> None:
    runtime, journal, path = _runtime(tmp_path)
    try:
        plan = _prepare(runtime)
        runtime.bind_provider_payload(plan.delivery_attempt_id, _payload(plan))

        loaded = load_runtime_attestations(path)

        assert loaded.integrity_ok is True
        assert len(loaded.attestations) == 1
        proof = loaded.attestations[0]
        assert proof.lifecycle_states == ("SELECTED", "COMPILED", "JOINED")
        assert proof.delivery_proven is False
        assert proof.response_committed is False
    finally:
        journal.close()


def test_reader_rejects_tampered_chain_instead_of_crediting_delivery(
    tmp_path,
) -> None:
    runtime, journal, path = _runtime(tmp_path)
    plan = _prepare(runtime)
    _complete(runtime, plan)
    journal.close()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER delivery_journal_no_update")
        connection.execute(
            """
            UPDATE delivery_journal
            SET provider_payload_hash = ?
            WHERE state = 'DELIVERED'
            """,
            ("0" * 64,),
        )
        connection.commit()

    loaded = load_runtime_attestations(path)

    assert loaded.journal_present is True
    assert loaded.integrity_ok is False
    assert loaded.attestations == ()
    assert any(
        item.reason == "DELIVERY_COLUMN_CANONICAL_MISMATCH"
        for item in loaded.rejected
    )


@pytest.mark.parametrize(
    ("field", "replacement", "persisted_column", "expected_reason"),
    [
        (
            "model_call_id",
            "call-forged",
            "model_call_id",
            "DELIVERY_IDENTITY_REWRITTEN",
        ),
        (
            "capsule_hash",
            "0" * 64,
            "capsule_hash",
            "DELIVERY_IDENTITY_REWRITTEN",
        ),
    ],
)
def test_reader_rejects_self_consistently_resealed_identity_rewrites(
    tmp_path,
    field,
    replacement,
    persisted_column,
    expected_reason,
) -> None:
    runtime, journal, path = _runtime(tmp_path)
    plan = _prepare(runtime)
    _complete(runtime, plan)
    journal.close()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER delivery_journal_no_update")
        row = connection.execute(
            """
            SELECT canonical_json
            FROM delivery_journal
            WHERE state = 'RESPONSE_COMMITTED'
            """
        ).fetchone()
        payload = json.loads(row[0])
        payload[field] = replacement
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            f"""
            UPDATE delivery_journal
            SET canonical_json = ?, state_hash = ?, {persisted_column} = ?
            WHERE state = 'RESPONSE_COMMITTED'
            """,
            (
                canonical,
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                replacement,
            ),
        )
        connection.commit()

    loaded = load_runtime_attestations(path)

    assert loaded.integrity_ok is False
    assert loaded.attestations == ()
    assert loaded.rejected[0].reason == expected_reason


def test_export_bundle_is_canonical_self_sealed_and_tamper_evident(
    tmp_path,
) -> None:
    runtime, journal, path = _runtime(tmp_path)
    try:
        plan = _prepare(runtime)
        _complete(runtime, plan)
    finally:
        journal.close()
    output = tmp_path / "runtime_attestation.json"

    bundle = export_runtime_attestations(path, output)
    reloaded = read_runtime_attestation_bundle(output)

    assert reloaded == bundle
    assert bundle.attestation_sha256
    assert output.read_text(encoding="utf-8").endswith("\n")

    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["records"][0]["delivery_proven"] = False
    output.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AttestationIntegrityError, match="seal|canonical"):
        read_runtime_attestation_bundle(output)


def test_absent_journal_is_inert_for_metrics_diagnostic(tmp_path) -> None:
    assert runtime_attestation_diagnostic(tmp_path) is None


def test_present_journal_diagnostic_never_claims_ack_or_influence(
    tmp_path,
) -> None:
    runtime, journal, _ = _runtime(tmp_path)
    try:
        plan = _prepare(runtime)
        _complete(runtime, plan)
    finally:
        journal.close()

    diagnostic = runtime_attestation_diagnostic(tmp_path)

    assert diagnostic is not None
    assert len(diagnostic["attestation_sha256"]) == 64
    assert diagnostic["delivered_count"] == 1
    assert diagnostic["response_committed_count"] == 1
    assert diagnostic["explicit_acknowledgment"] == "UNMEASURED"
    assert diagnostic["behavioral_influence"] == "UNMEASURED"
