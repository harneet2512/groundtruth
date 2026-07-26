"""Wave-7 RED contract for producer-owned canonical evidence attachments.

The conversion boundary added in Wave 6 is intentionally correct-or-quiet: it
must not infer decision semantics from legacy ``payload`` prose.  That means the
live producers, rather than the converter, must carry an explicit typed
``CanonicalEvidenceSemantics`` sidecar.  This suite pins that attachment
contract without changing any producer implementation.

The realistic route is Gateway ``augment`` -> ``def_ref_partition``.  The
remaining rows exercise the exact ten model-facing FACT registrations at the
shared envelope/conversion boundary.  CAP byte-owner identities are audit
lineage on the same physical FACT; they never create extra records.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import fact_registry
from groundtruth.runtime import feature_lineage
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    DefinitionRow,
    ProducerInputs,
)


REVISION = rr.RevisionVector(
    repository_content="repo-content-17",
    graph="graph-17",
    lsp="lsp-17",
    runtime_evidence="runtime-17",
)

FACT_IDS = tuple(
    sorted(
        feature_id
        for feature_id, registration in fact_registry.REGISTRY.items()
        if registration.fact_role == fact_registry.FACT_ROLE_DELIVERY
    )
)

TYPED_CAP_OWNER_CASES = (
    (
        "newfile_precedent",
        "change_surface",
        "missing_role",
        "GT_CHANGE_SURFACE",
    ),
    (
        "signature_delta",
        "patch_delta",
        "companion_surface",
        "GT_PATCH_DELTA",
    ),
    (
        "localization",
        "ranked_localization",
        "localization",
        "GT_LOC_RESLOT",
    ),
)


def _semantics(fact_class: str, *, revision: rr.RevisionVector = REVISION):
    contract = rr.feature_contract_for(fact_class)
    assert contract is not None
    return rr.CanonicalEvidenceSemantics(
        decision_context=contract.decision_context,
        roles=contract.roles,
        claim=f"{fact_class} producer claim",
        actionable_consequence=f"{fact_class} producer consequence",
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            f"fact:{fact_class}",
        ),
        authority=rr.Authority.RESULT_DERIVED,
        revision=revision,
        revision_dependencies=contract.revision_dependencies,
        mandatory_reason=None,
        failure_prevention=4,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
    )


def _attach_explicit_semantics(
    envelope: ee.EvidenceEnvelope,
    semantics: rr.CanonicalEvidenceSemantics,
) -> ee.EvidenceEnvelope:
    """Use the public dataclass attachment field the live producers must own."""

    return replace(envelope, canonical_semantics=semantics)


def _registered_envelope(
    fact_class: str,
    *,
    producer: str | None = None,
    evidence_type: str | None = None,
    cap_feature_ids: tuple[str, ...] = (),
    revision: rr.RevisionVector = REVISION,
    attach_semantics: bool = True,
) -> ee.EvidenceEnvelope:
    registration = fact_registry.REGISTRY[fact_class]
    runtime_producer = producer or registration.producer
    layer = evidence_type or fact_class
    actual_event = fact_registry.required_event(layer)
    assert actual_event is not None
    lineage = feature_lineage.build_lineage(
        runtime_producer_id=runtime_producer,
        evidence_type=layer,
        actual_event=actual_event,
        cap_feature_ids=cap_feature_ids,
    )
    assert lineage is not None
    envelope = ee.EvidenceEnvelope.build(
        producer=runtime_producer,
        fact_id=f"physical:{fact_class}",
        target=f"src/product/{fact_class}.py::subject",
        evidence_type=layer,
        payload=("legacy payload remains render-only",),
        provenance=((f"src/product/{fact_class}.py", 17),),
        confidence=0.91,
        tier=ee.VERIFIED,
        graph_revision=revision.graph,
        valid_until=revision.graph,
        preferred_event=ee.EVENT_VIEW,
        estimated_cost_tokens=40,
        lineage=lineage,
        # Existing producer inputs have a different purpose.  Canonical
        # semantics must not overwrite or masquerade as this sidecar.
        producer_inputs={"structured": "producer-owned-inputs"},
    )
    if attach_semantics:
        envelope = _attach_explicit_semantics(
            envelope, _semantics(fact_class, revision=revision)
        )
    return envelope


def _write_ambiguous_graph(tmp_path) -> str:
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
            (1, "Function", "run", "a/x.py", 10, 15, 0, "python"),
            (2, "Function", "run", "b/y.py", 20, 25, 0, "python"),
        ),
    )
    connection.commit()
    connection.close()
    return db


def test_semantics_is_an_explicit_identity_neutral_sidecar() -> None:
    base = _registered_envelope("caller_contract", attach_semantics=False)
    before_dict = ee.to_dict(base)
    before_bytes = ee.render_bytes(base)
    before_dedup = base.dedup_key
    producer_inputs = base.producer_inputs

    attached = _attach_explicit_semantics(
        base, _semantics("caller_contract")
    )

    assert isinstance(
        attached.canonical_semantics, rr.CanonicalEvidenceSemantics
    )
    assert attached.producer_inputs is producer_inputs
    assert ee.to_dict(attached) == before_dict
    assert ee.render_bytes(attached) == before_bytes
    assert attached.dedup_key == before_dedup
    assert attached == base


def test_all_ten_fact_computations_convert_from_explicit_typed_semantics() -> None:
    assert len(FACT_IDS) == 10

    records = rr.canonicalize_evidence_envelopes(
        tuple(_registered_envelope(fact_class) for fact_class in FACT_IDS)
    )

    assert len(records) == 10
    assert {record.feature_id for record in records} == set(FACT_IDS)
    assert all(record.revision == REVISION for record in records)
    assert all(
        record.revision_dependencies
        == rr.feature_contract_for(record.feature_id).revision_dependencies
        for record in records
    )


def test_real_gateway_def_partition_attaches_full_canonical_semantics(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    graph_db = _write_ambiguous_graph(tmp_path)
    state = gateway.GatewayState(
        graph_db=graph_db,
        repo_root=str(tmp_path),
    )
    # The live Attempt Runtime owns the complete revision vector.  The Gateway
    # receives that canonical context; it must not reconstruct missing
    # repository/LSP/runtime revisions from its legacy graph-only token.
    state.canonical_revision = REVISION
    event = gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        carrier_kind=gateway.KIND_SEARCH,
        command="rg run",
        output="a/x.py:10: run\nb/y.py:20: run",
        action_index=1,
        semantic_events=("search_result",),
        primary_boundary="search_result",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )

    envelopes = gateway.augment(event, state)

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.evidence_type == "def_ref_partition"
    assert isinstance(envelope.producer_inputs, ProducerInputs)
    assert envelope.producer_inputs.schema == PRODUCER_INPUTS_SCHEMA
    assert envelope.producer_inputs.definition_rows == (
        DefinitionRow("run", "a/x.py", 10, "Function", 1),
        DefinitionRow("run", "b/y.py", 20, "Function", 2),
    )
    assert isinstance(
        envelope.canonical_semantics, rr.CanonicalEvidenceSemantics
    )
    assert envelope.canonical_semantics.revision == REVISION
    assert envelope.lineage is not None
    assert envelope.lineage.producer_registration_match is True
    assert envelope.lineage.fact_class == "def_partition"

    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.feature_id == "def_partition"
    assert record.revision == REVISION
    assert record.claim == envelope.canonical_semantics.claim
    assert "run" in record.claim
    assert "production definition" in record.claim
    assert "active execution path" in record.actionable_consequence
    assert "evidence is available" not in record.claim


@pytest.mark.parametrize(
    "fact_class,producer,evidence_type,owner_id",
    TYPED_CAP_OWNER_CASES,
)
def test_authorized_typed_cap_owner_is_audit_lineage_on_one_fact(
    fact_class: str,
    producer: str,
    evidence_type: str,
    owner_id: str,
) -> None:
    envelope = _registered_envelope(
        fact_class,
        producer=producer,
        evidence_type=evidence_type,
        cap_feature_ids=(owner_id,),
    )

    record = rr.canonical_evidence_from_envelope(envelope)

    assert record is not None
    assert record.feature_id == fact_class
    assert record.owner_feature_ids == (owner_id,)
    assert owner_id not in record.claim
    assert owner_id not in record.actionable_consequence


def test_unknown_legacy_untyped_and_revision_crossed_envelopes_stay_quiet() -> None:
    legacy = _registered_envelope("caller_contract", attach_semantics=False)
    unknown = ee.EvidenceEnvelope.build(
        producer="legacy_unknown",
        fact_id="unknown",
        target="src/product/unknown.py",
        evidence_type="legacy_unknown",
        payload=("unknown",),
        provenance=(("src/product/unknown.py", 1),),
        confidence=0.9,
        tier=ee.VERIFIED,
        graph_revision=REVISION.graph,
    )
    crossed_revision = _registered_envelope(
        "caller_contract",
        revision=rr.RevisionVector(
            repository_content=REVISION.repository_content,
            graph="different-graph",
            lsp=REVISION.lsp,
            runtime_evidence=REVISION.runtime_evidence,
        ),
    )
    # The legacy envelope declares a different graph identity than the attached
    # full vector.  A converter must not silently reconcile the contradiction.
    crossed_revision = replace(
        crossed_revision,
        graph_revision=REVISION.graph,
        valid_until=REVISION.graph,
    )

    assert rr.canonicalize_evidence_envelopes(
        (legacy, unknown, crossed_revision)
    ) == ()
