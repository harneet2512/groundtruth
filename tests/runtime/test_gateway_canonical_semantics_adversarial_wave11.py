"""Adversarial contracts for Gateway-owned canonical decision semantics.

These tests exercise the real producer boundary, not a synthetic envelope
factory.  A producer may keep its legacy payload for the established native
render/dedup path, but canonical scheduling is allowed only when that producer
also supplies a specific claim and actionable consequence derived from its
structured computation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from groundtruth.pretask.change_surface import (
    ChangeSurfaceResult,
    MissingRole,
    NewFileDestination,
)
from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.patch_delta import (
    CompanionSurface,
    PatchDeltaResult,
    SignatureMismatch,
)


REVISION = rr.RevisionVector(
    repository_content="repo-wave11",
    graph="graph-wave11",
    lsp="lsp-wave11",
    runtime_evidence="runtime-wave11",
)


def _state(tmp_path, *, graph_db: str | None = None) -> gateway.GatewayState:
    state = gateway.GatewayState(
        graph_db=graph_db,
        repo_root=str(tmp_path),
        issue_text="Add an azure provider and preserve refresh_session callers",
    )
    state.canonical_revision = REVISION
    return state


def _event(
    kind: str,
    boundary: str,
    *,
    command: str = "",
    output: str = "",
    edit_before_after=None,
    covering=None,
) -> gateway.ToolEvent:
    return gateway.ToolEvent(
        kind=kind,
        carrier_kind=kind,
        command=command,
        output=output,
        action_index=7,
        edit_before_after=edit_before_after,
        covering=covering,
        semantic_events=(boundary,),
        primary_boundary=boundary,
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )


def _record(envelope: ee.EvidenceEnvelope) -> rr.EvidenceRecord:
    semantics = envelope.canonical_semantics
    assert isinstance(semantics, rr.CanonicalEvidenceSemantics)
    assert semantics.revision == REVISION
    assert semantics.claim.strip()
    assert semantics.actionable_consequence.strip()
    assert "evidence available" not in semantics.claim.lower()
    assert "evidence is available" not in semantics.claim.lower()

    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.claim == semantics.claim
    assert record.actionable_consequence == semantics.actionable_consequence
    return record


def _assert_identity_neutral(envelope: ee.EvidenceEnvelope) -> None:
    """The typed sidecar must not change legacy bytes, JSON, equality or dedup."""

    assert envelope.canonical_semantics is not None
    legacy_view = replace(envelope, canonical_semantics=None)
    assert ee.render_bytes(envelope) == ee.render_bytes(legacy_view)
    assert ee.to_dict(envelope) == ee.to_dict(legacy_view)
    assert envelope.dedup_key == legacy_view.dedup_key
    assert envelope == legacy_view


def _definition_graph(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE nodes("
        "id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT, "
        "file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, "
        "return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT, "
        "parent_id INTEGER);"
        "CREATE TABLE edges("
        "id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT, "
        "source_line INTEGER, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT);"
    )
    connection.executemany(
        "INSERT INTO nodes("
        "id,label,name,file_path,start_line,end_line,is_test,language"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            (1, "Function", "refresh_session", "src/auth.py", 11, 20, 0, "python"),
            (2, "Function", "refresh_session", "legacy/auth.py", 31, 40, 0, "python"),
        ),
    )
    connection.commit()
    connection.close()
    return db


def test_def_partition_semantics_name_the_resolved_symbol_and_edit_constraint(
    tmp_path,
) -> None:
    graph_db = _definition_graph(tmp_path)
    state = _state(tmp_path, graph_db=graph_db)
    event = _event(
        gateway.KIND_SEARCH,
        "search_result",
        command="rg refresh_session",
    )

    envelopes = gateway._produce_def_ref_partition(event, state)

    assert len(envelopes) == 1
    record = _record(envelopes[0])
    assert record.feature_id == "def_partition"
    assert "refresh_session" in record.claim
    assert "2 production definition site(s)" in record.claim
    assert "active execution path" in record.actionable_consequence
    assert "do not patch" in record.actionable_consequence
    _assert_identity_neutral(envelopes[0])


def test_ranked_localization_semantics_preserve_rank_and_next_action(
    tmp_path, monkeypatch
) -> None:
    rows = [
        ("src/auth/session.py", 41, "refresh_session"),
        ("src/auth/token.py", 19, "rotate_token"),
    ]
    # signature grew an optional ProducerAudit (abstention telemetry)
    monkeypatch.setattr(
        gateway, "_ranked_localization_rows", lambda _state, _audit=None: rows
    )
    state = _state(tmp_path)
    event = _event(
        gateway.KIND_SEARCH,
        "search_result",
        command="rg 'rotation behavior'",
    )

    envelopes = gateway._produce_ranked_localization(event, state)

    assert len(envelopes) == 1
    record = _record(envelopes[0])
    assert record.feature_id == "localization"
    assert "src/auth/session.py" in record.claim
    assert "highest-ranked production target" in record.claim
    assert "2 ranked candidate file(s)" in record.claim
    assert "listed symbol" in record.actionable_consequence
    assert envelopes[0].native_args == {"rows": rows}
    _assert_identity_neutral(envelopes[0])


def test_signature_and_companion_computations_have_distinct_actionable_semantics(
    tmp_path, monkeypatch
) -> None:
    signature = SignatureMismatch(
        symbol="refresh_session",
        edited_file="src/auth/session.py",
        old_min_params=1,
        old_max_params=1,
        new_min_params=2,
        new_max_params=2,
        caller="refresh_request",
        caller_file="src/auth/middleware.py",
        caller_line=27,
        call_site_text="refresh_session(token)",
        positional_args=1,
        confidence=0.95,
        resolution_method="import",
        before_sha256="a" * 64,
        after_sha256="b" * 64,
        caller_source_sha256="c" * 64,
        edge_id=17,
        definition_id=9,
    )
    companion = CompanionSurface(
        symbol="AzureProvider",
        edited_file="providers/azure.py",
        file="providers/__init__.py",
        siblings=("AwsProvider", "GcpProvider"),
        referencing_lines=((5, "AwsProvider,"), (6, "GcpProvider,")),
    )
    monkeypatch.setattr(
        gateway,
        "analyze_patch_delta",
        lambda *_args, **_kwargs: PatchDeltaResult(
            signature_mismatches=[signature],
            companion_surfaces=[companion],
        ),
    )
    event = _event(
        gateway.KIND_EDIT,
        "edit_result",
        command="apply_patch",
        edit_before_after={
            "src/auth/session.py": (
                "def refresh_session(token):\n    return token\n",
                "def refresh_session(token, store):\n    return token\n",
            )
        },
    )

    envelopes = gateway._produce_patch_delta(event, _state(tmp_path))

    assert {envelope.evidence_type for envelope in envelopes} == {
        "signature_mismatch",
        "companion_surface",
    }
    signature_env = next(
        envelope
        for envelope in envelopes
        if envelope.evidence_type == "signature_mismatch"
    )
    companion_env = next(
        envelope
        for envelope in envelopes
        if envelope.evidence_type == "companion_surface"
    )

    signature_record = _record(signature_env)
    assert signature_record.feature_id == "signature_delta"
    assert "src/auth/middleware.py:27" in signature_record.claim
    assert "1 positional argument(s)" in signature_record.claim
    assert "accepts 2-2" in signature_record.claim
    assert "update the impacted caller" in signature_record.actionable_consequence.lower()
    assert "restore a compatible signature" in (
        signature_record.actionable_consequence.lower()
    )

    companion_record = _record(companion_env)
    assert companion_record.feature_id == "signature_delta"
    assert "providers/__init__.py" in companion_record.claim
    assert "does not register AzureProvider" in companion_record.claim
    assert "companion registration surface" in (
        companion_record.actionable_consequence
    )
    _assert_identity_neutral(signature_env)
    _assert_identity_neutral(companion_env)


def test_new_file_destination_without_source_witness_stays_canonical_quiet(
    tmp_path, monkeypatch
) -> None:
    result = ChangeSurfaceResult(
        entities=["azure"],
        destinations=[
            NewFileDestination(
                entity="azure",
                suggested_path="providers/azure.py",
                directory="providers",
                template_file="providers/aws.py",
                registration_file="providers/__init__.py",
                sibling_files=["providers/aws.py", "providers/gcp.py"],
                issue_span="add an azure provider",
                evidence=["parallel sibling family: aws, gcp"],
            )
        ],
        abstained=False,
    )
    monkeypatch.setattr(gateway, "detect_change_surface", lambda *_args: result)
    event = _event(
        gateway.KIND_SEARCH,
        "search_result",
        command="rg azure",
    )

    envelopes = gateway._produce_change_surface(
        event, _state(tmp_path), require_repeat=False
    )

    assert len(envelopes) == 1
    envelope = envelopes[0]
    semantics = envelope.canonical_semantics
    assert isinstance(semantics, rr.CanonicalEvidenceSemantics)
    assert "providers/azure.py" in semantics.claim
    assert "missing azure surface" in semantics.claim
    assert "sibling/template" in semantics.actionable_consequence
    assert "registration precedent" in semantics.actionable_consequence
    assert "template: providers/aws.py" in envelope.payload
    assert "integrate at: providers/__init__.py" in envelope.payload
    # The destination detector currently supplies no source line witness for
    # destinations.  Canonical conversion must remain quiet rather than invent
    # a path:line provenance row.  A witnessed missing-role result below proves
    # the same FACT class is live when its computation is auditable.
    assert envelope.provenance == ()
    assert rr.canonical_evidence_from_envelope(envelope) is None
    _assert_identity_neutral(envelope)


def test_witnessed_new_file_role_semantics_name_missing_role_and_integration_action(
    tmp_path, monkeypatch
) -> None:
    result = ChangeSurfaceResult(
        entities=["azure"],
        missing_roles=[
            MissingRole(
                role="export",
                entity="azure",
                sibling_files=["providers/aws.py", "providers/gcp.py"],
                registration_file="providers/__init__.py",
                registration_lines=[
                    (5, "from .aws import AwsProvider"),
                    (6, "from .gcp import GcpProvider"),
                ],
                issue_span="add an azure provider",
                signals=["parallel siblings", "shared export surface"],
                evidence=["providers/__init__.py exports aws and gcp siblings"],
            )
        ],
        abstained=False,
    )
    monkeypatch.setattr(gateway, "detect_change_surface", lambda *_args: result)
    event = _event(
        gateway.KIND_SEARCH,
        "search_result",
        command="rg azure",
    )

    envelopes = gateway._produce_change_surface(
        event, _state(tmp_path), require_repeat=False
    )

    assert len(envelopes) == 1
    record = _record(envelopes[0])
    assert record.feature_id == "newfile_precedent"
    assert "azure is missing the repository role export" in record.claim
    assert "providers/__init__.py" in record.claim
    assert "complete the missing companion or registration role" in (
        record.actionable_consequence.lower()
    )
    assert record.provenance == (
        "providers/__init__.py:5",
        "providers/__init__.py:6",
    )
    _assert_identity_neutral(envelopes[0])


def test_covering_semantics_tie_executed_verdict_to_patch_validation(
    tmp_path,
) -> None:
    covering = gateway.CoveringResult(
        target="src/auth/session.py",
        verdict="RED",
        body_lines=[
            "E       AssertionError: rotated session was not returned",
            "E       assert 'old' == 'rotated'",
        ],
        evidence=[("src/auth/session.py", 41)],
        tier=ee.WARNING,
    )
    event = _event(
        gateway.KIND_TEST,
        "test_result",
        command="pytest -q",
        covering=covering,
    )

    envelopes = gateway._produce_covering(event, _state(tmp_path))

    assert len(envelopes) == 1
    record = _record(envelopes[0])
    assert record.feature_id == "covering_red"
    assert "executed covering validation" in record.claim
    assert "src/auth/session.py" in record.claim
    assert "verdict RED" in record.claim
    assert "validation constraint" in record.actionable_consequence
    _assert_identity_neutral(envelopes[0])


def test_registered_producer_without_owned_meaning_stays_canonical_quiet(
    tmp_path,
) -> None:
    """A registered identity alone must not authorize a placeholder claim."""

    state = _state(tmp_path)
    event = _event(gateway.KIND_SEARCH, "search_result", command="rg refresh_session")
    envelope = gateway._mk_add(
        state,
        event,
        fact_kind="def_ref_partition",
        target="src/auth/session.py",
        body_lines=["def: src/auth/session.py:41"],
        evidence=[("src/auth/session.py", 41)],
        tier=ee.VERIFIED,
        producer="def_ref_partition",
        symbol="refresh_session",
        canonical_claim="",
        canonical_consequence="",
    )

    assert envelope.canonical_semantics is None
    assert rr.canonical_evidence_from_envelope(envelope) is None
    assert ee.render_bytes(envelope)
    assert "evidence available" not in ee.render_bytes(envelope).decode().lower()


@pytest.mark.parametrize(
    "field,value",
    (
        ("canonical_claim", ""),
        ("canonical_consequence", ""),
    ),
)
def test_mk_add_requires_both_halves_of_decision_meaning(
    tmp_path, field: str, value: str
) -> None:
    kwargs = {
        "canonical_claim": "refresh_session resolves to its production definition.",
        "canonical_consequence": "Inspect that definition before editing.",
    }
    kwargs[field] = value
    envelope = gateway._mk_add(
        _state(tmp_path),
        _event(gateway.KIND_SEARCH, "search_result", command="rg refresh_session"),
        fact_kind="def_ref_partition",
        target="src/auth/session.py",
        body_lines=["def: src/auth/session.py:41"],
        evidence=[("src/auth/session.py", 41)],
        tier=ee.VERIFIED,
        producer="def_ref_partition",
        symbol="refresh_session",
        **kwargs,
    )

    assert envelope.canonical_semantics is None
    assert rr.canonical_evidence_from_envelope(envelope) is None
