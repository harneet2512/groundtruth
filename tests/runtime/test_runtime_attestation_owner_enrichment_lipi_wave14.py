"""Attestation must preserve canonical generation identity during owner enrichment."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime import runtime_attestation as attestation


REVISION = rr.RevisionVector(
    repository_content="repo-owner-enrichment",
    graph="graph-owner-enrichment",
    lsp="lsp-owner-enrichment",
    runtime_evidence="runtime-owner-enrichment",
)


def _evidence(
    *,
    owners: tuple[str, ...] = (),
    claim: str = "The current edit is structurally invalid.",
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("syntax_result")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id="GT-E-owner-enrichment",
        feature_id="syntax_result",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="src/module.py",
        claim=claim,
        actionable_consequence="Repair syntax before propagating the patch.",
        provenance=("src/module.py:9",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=("path:src/module.py",),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=rr.MandatoryReason.BLOCKER,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.RESULT_DERIVED,
        owner_feature_ids=owners,
    )


def _connection(*records: rr.EvidenceRecord) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE evidence_attempt_journal (
            attempt_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            journal_sequence INTEGER NOT NULL,
            lifecycle TEXT NOT NULL,
            state_hash TEXT NOT NULL,
            canonical_json TEXT NOT NULL
        )
        """
    )
    for sequence, record in enumerate(records, start=1):
        payload = record.canonical_json()
        connection.execute(
            """
            INSERT INTO evidence_attempt_journal(
                attempt_id, evidence_id, journal_sequence, lifecycle,
                state_hash, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "attempt-owner-enrichment",
                record.evidence_id,
                sequence,
                record.lifecycle.value,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                payload,
            ),
        )
    return connection


def _load(connection: sqlite3.Connection):
    return attestation._load_evidence_ownership(
        connection,
        attempt_id="attempt-owner-enrichment",
        evidence_ids=("GT-E-owner-enrichment",),
        require_delivered=False,
    )


def test_attestation_accepts_authorized_monotonic_owner_enrichment() -> None:
    connection = _connection(
        _evidence(),
        _evidence(owners=("GT_EDIT_CHECK",)),
    )
    try:
        records, _ = _load(connection)
    finally:
        connection.close()

    assert records[0].owner_feature_ids == ("GT_EDIT_CHECK",)


def test_attestation_rejects_semantic_rewrite_disguised_as_owner_enrichment() -> None:
    connection = _connection(
        _evidence(),
        _evidence(
            owners=("GT_EDIT_CHECK",),
            claim="A resealed but different canonical claim.",
        ),
    )
    try:
        with pytest.raises(
            attestation._Reject,
            match="EVIDENCE_GENERATION_REWRITTEN",
        ):
            _load(connection)
    finally:
        connection.close()


def test_attestation_rejects_owner_not_authorized_for_fact() -> None:
    connection = _connection(
        replace(
            _evidence(),
            owner_feature_ids=("GT_PATCH_DELTA",),
        ),
    )
    try:
        with pytest.raises(
            attestation._Reject,
            match="EVIDENCE_OWNERSHIP_INVALID",
        ):
            _load(connection)
    finally:
        connection.close()


def test_attestation_rejects_impossible_lifecycle_history() -> None:
    impossible = replace(
        _evidence(),
        lifecycle=rr.EvidenceLifecycle.ACTIVE,
        transition_history=(
            rr.EvidenceTransition(
                from_state=rr.EvidenceLifecycle.PENDING,
                to_state=rr.EvidenceLifecycle.ACTIVE,
                reason_code=(
                    rr.EvidenceTransitionReason.ACTIVATED_AFTER_PROVIDER_DELIVERY
                ),
            ),
        ),
    )
    connection = _connection(_evidence(), impossible)
    try:
        with pytest.raises(
            attestation._Reject,
            match="EVIDENCE_TRANSITION_HISTORY_INVALID",
        ):
            _load(connection)
    finally:
        connection.close()
