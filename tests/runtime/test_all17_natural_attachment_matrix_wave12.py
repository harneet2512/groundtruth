"""Executable attachment matrix for the ten FACTs and seven CAP byte owners.

This is deliberately not another synthetic ``EvidenceEnvelope`` census.  Each
FACT enters through the closest deterministic runtime boundary available:

* sealed task-start brief receipts;
* structured Gateway search/edit observations over a real repository/graph;
* the attempt-scoped deep validation hook; or
* the submit Gate Kernel producer.

The final assertions keep three claims separate:

``produced`` -> ``canonical record`` -> ``ingested/released/provider staged``.

CAP rows are byte-owner lineage on the same physical FACT.  They never become
additional evidence records or additional capsules.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import fact_registry
from groundtruth.runtime import feature_lineage
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.canonical_producers import (
    ProducerContext,
    SubmitEvidenceOwner,
    produce_submit_refusal,
)
from groundtruth.runtime.covering_runner import CoveringAttribution
from groundtruth.runtime.miniswe_provider_boundary import MiniSweProviderBoundary
from groundtruth.runtime.submit_gate import (
    safe_build_certificate,
    safe_gate_verdict,
)


REVISION = rr.RevisionVector(
    repository_content="repo-all17",
    graph="graph-all17",
    lsp="lsp-all17",
    runtime_evidence="runtime-all17",
)
RUNTIME_EVENT_HASH = "e" * 64


def _internal_event(component: str, outcomes=()):
    return SimpleNamespace(
        event_id=f"attempt-matrix:internal:{component}",
        content_hash=RUNTIME_EVENT_HASH,
        outcomes=tuple(outcomes),
    )


def _canonicalize_runtime(envelopes):
    return rr.canonicalize_evidence_envelopes(
        envelopes,
        committed_event_hashes={
            witness.witness_id: witness.content_sha256
            for envelope in envelopes
            for witness in envelope.runtime_witnesses
            if witness.kind == "canonical_event"
        },
    )

FACT_IDS = frozenset(
    feature_id
    for feature_id, registration in fact_registry.REGISTRY.items()
    if registration.fact_role == fact_registry.FACT_ROLE_DELIVERY
)
OWNER_TO_FACT = {
    "GT_CHANGE_SURFACE": "newfile_precedent",
    "GT_PATCH_DELTA": "signature_delta",
    "GT_LOC_RESLOT": "localization",
    "GT_SS_SUBMIT_RED": "submit_refusal",
    "GT_EDIT_CHECK": "syntax_result",
    "GT_HYPOTHESIS": "recovery",
    "GT_CERT_DELIVERY": "submit_refusal",
}
ORIGIN_ASSURANCE = {
    "caller_contract": "structured_gateway_real_graph",
    "covering_red": "deep_hook_stubbed_engine_result",
    "def_partition": "structured_gateway_real_graph",
    "localization": "structured_gateway_stubbed_localizer",
    "newfile_precedent": "structured_gateway_real_repository",
    "obligations": "sealed_task_start_fixture",
    "recovery": "deep_hook_stubbed_engine_result",
    "signature_delta": "structured_gateway_real_graph",
    "submit_refusal": "structured_submit_host_boundary",
    "syntax_result": "deep_hook_stubbed_engine_result",
}

READY_PREDICATES = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
        rr.TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,
    }
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _receipt(
    brief: str,
    block: str,
    *,
    block_id: str,
    fact_class: str,
    candidate_id: str,
) -> dict[str, object]:
    start = brief.index(block)
    return {
        "block_id": block_id,
        "label": block_id,
        "fact_class": fact_class,
        "candidate_id": candidate_id,
        "char_span": [start, start + len(block)],
        "content_hash": hashlib.sha256(block.encode("utf-8")).hexdigest(),
    }


def _brief_records(root: Path) -> tuple[rr.EvidenceRecord, ...]:
    root.mkdir(parents=True, exist_ok=True)
    localization = "1. src/api.py\n   get_user is on the active path."
    obligations = (
        "<gt-obligations>\n"
        "- preserve the caller-visible return value\n"
        "</gt-obligations>"
    )
    brief = (
        "<gt-task-brief>\n"
        f"{localization}\n"
        f"{obligations}\n"
        "</gt-task-brief>"
    )
    loc_receipt = _receipt(
        brief,
        localization,
        block_id="file-1",
        fact_class="localization",
        candidate_id="loc-all17",
    )
    obligation_receipt = _receipt(
        brief,
        obligations,
        block_id="obligations",
        fact_class="obligations",
        candidate_id="obl-all17",
    )
    brief_path = root / "brief.txt"
    brief_path.write_text(brief, encoding="utf-8", newline="")
    (root / "brief_result.json").write_text(
        json.dumps(
            {
                "schema": "gt.brief_result.v1",
                "brief_text": brief,
                "metrics": {
                    "block_receipts": [loc_receipt, obligation_receipt],
                    "localization_proof": [
                        {
                            "candidate_id": "loc-all17",
                            "path": "src/api.py",
                            "witness": "resolved import path",
                            "witness_verified": True,
                        }
                    ],
                    "obligations_record": {
                        "schema": "gt.obligations_record.v1",
                        "candidate_id": "obl-all17",
                        "block_content_sha256": obligation_receipt[
                            "content_hash"
                        ],
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return seam._canonical_brief_records(
        {"GT_BRIEF_FILE": str(brief_path)},
        REVISION,
    )


def _graph(root: Path) -> Path:
    _write(root, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(root, "src/alt.py", "def get_user(uid):\n    return uid\n")
    _write(root, "src/caller.py", "def use():\n    return get_user(1)\n")
    db = root / "graph.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE nodes("
        "id INTEGER PRIMARY KEY,label TEXT,name TEXT,qualified_name TEXT,"
        "file_path TEXT,start_line INTEGER,end_line INTEGER,signature TEXT,"
        "return_type TEXT,is_exported INTEGER,is_test INTEGER,language TEXT,"
        "parent_id INTEGER);"
        "CREATE TABLE edges("
        "id INTEGER PRIMARY KEY,source_id INTEGER,target_id INTEGER,type TEXT,"
        "source_line INTEGER,source_file TEXT,resolution_method TEXT,"
        "confidence REAL,metadata TEXT);"
        "INSERT INTO nodes VALUES"
        "(1,'Function','get_user',NULL,'src/api.py',1,2,NULL,NULL,0,0,'python',NULL),"
        "(2,'Function','get_user',NULL,'src/alt.py',1,2,NULL,NULL,0,0,'python',NULL),"
        "(3,'Function','use',NULL,'src/caller.py',1,2,NULL,NULL,0,0,'python',NULL);"
        "INSERT INTO edges VALUES"
        "(11,3,1,'CALLS',2,'src/caller.py','import',0.95,NULL);"
    )
    connection.commit()
    connection.close()
    return db


def _gateway_records(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[rr.EvidenceRecord, ...]:
    db = _graph(root)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    monkeypatch.setattr(
        gateway,
        "_compute_ranked_localization_rows",
        lambda _state: [("src/api.py", 1, "get_user")],
    )
    state = gateway.GatewayState(
        graph_db=str(db),
        repo_root=str(root),
        graph_revision=REVISION.graph,
        issue_text="Preserve get_user while adding its second parameter.",
    )
    state.canonical_revision = REVISION

    search = gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        carrier_kind=gateway.KIND_SEARCH,
        command="rg get_user",
        output="src/api.py:1:get_user\nsrc/alt.py:1:get_user",
        action_index=1,
        semantic_events=("search_result",),
        primary_boundary="search_result",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )
    before = "def get_user(uid):\n    return uid\n"
    after = "def get_user(uid, name):\n    return uid\n"
    edit = gateway.ToolEvent(
        kind=gateway.KIND_EDIT,
        carrier_kind=gateway.KIND_EDIT,
        command="structured edit",
        action_index=2,
        changed_files=("src/api.py",),
        edit_before_after={"src/api.py": (before, after)},
        semantic_events=("edit_result",),
        primary_boundary="edit_result",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )
    envelopes = (
        *gateway.produce_raw(search, state),
        *gateway.produce_raw(edit, state),
    )
    return rr.canonicalize_evidence_envelopes(envelopes)


def _newfile_record(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> rr.EvidenceRecord:
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _write(
        root,
        "providers/__init__.py",
        "from .aws import AwsProvider\n"
        "from .gcp import GcpProvider\n"
        "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n",
    )
    _write(root, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _write(root, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    db = root / "graph.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE nodes("
        "id INTEGER PRIMARY KEY,label TEXT,name TEXT,file_path TEXT,"
        "start_line INTEGER,is_test INTEGER,language TEXT)"
    )
    connection.commit()
    connection.close()
    state = gateway.GatewayState(
        graph_db=str(db),
        repo_root=str(root),
        issue_text="Add an azure provider like the aws and gcp providers.",
    )
    state.canonical_revision = REVISION
    first = gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        carrier_kind=gateway.KIND_SEARCH,
        command="rg azure",
        output="",
        exit_status=1,
        action_index=1,
        semantic_events=("search_result", "failed_search"),
        primary_boundary="failed_search",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )
    second = gateway.ToolEvent(
        **{
            **first.__dict__,
            "action_index": 2,
        }
    )
    first_records = rr.canonicalize_evidence_envelopes(
        gateway.produce_raw(first, state)
    )
    assert all(
        record.feature_id != "newfile_precedent"
        for record in first_records
    )
    records = rr.canonicalize_evidence_envelopes(
        gateway.produce_raw(second, state)
    )
    return next(
        record
        for record in records
        if record.feature_id == "newfile_precedent"
    )


def _deep_records(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[rr.EvidenceRecord, ...]:
    work_state = SimpleNamespace(
        phase=rr.Phase.RECOVERY,
        focused_symbols=("get_user",),
        focused_files=("src/api.py",),
        edited_files=("src/api.py",),
        current_failures=("same-failure",),
        transition_rules=("repeated_failure_after_edit",),
    )
    attachment = seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=SimpleNamespace(
            attempt_id="attempt-matrix",
            work_state=work_state,
            journal=SimpleNamespace(
                events=lambda _attempt_id: (
                    _internal_event(
                        "recovery-source",
                        (
                            rr.SemanticOutcome(
                                kind=rr.SemanticKind.TEST_FAIL,
                                subject="tests/test_api.py",
                                failure_fingerprint="same-failure",
                            ),
                        ),
                    ),
                )
            ),
        ),
        provider_boundary=None,
        gateway_state=None,
        graph_revision=REVISION.graph,
    )
    monkeypatch.setattr(
        attachment,
        "_append_internal_validation_event",
        lambda **kwargs: _internal_event(
            kwargs["component"],
            kwargs["outcomes"],
        ),
    )
    monkeypatch.setattr(seam, "_root", lambda: "/repo")
    monkeypatch.setattr(seam, "_db_path", lambda: "/repo/graph.db")
    monkeypatch.setattr(seam, "_build_edit_check_executor", lambda: None)
    monkeypatch.setattr(seam, "_build_verification_executor", lambda: None)

    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.delenv("GT_HYPOTHESIS", raising=False)
    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *_args, **_kwargs: {
            "verdict": "syntax_error",
            "reason": "parse_error",
            "diagnostic": "src/api.py:1: SyntaxError: invalid syntax",
            "checker": ["ast.parse"],
        },
    )
    syntax = attachment._deep_reactive_envelopes(
        changed_files=("src/api.py",),
        revision=REVISION,
    )

    monkeypatch.delenv("GT_EDIT_CHECK", raising=False)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    result = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/test_api.py"],
        "ran": ["tests/test_api.py"],
        "command": ["pytest", "-q", "tests/test_api.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_get_user"],
    }
    attribution = CoveringAttribution(
        attributed=True,
        method="unresolved_covering",
        current_verdict="fail",
        base_verdict="fail",
        implicated_edited_paths=("src/api.py",),
        covering_files=("tests/test_api.py",),
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.select_covering_tests",
        lambda *_args, **_kwargs: [{"file": "tests/test_api.py"}],
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.run_covering_tests",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.attribute_covering_red",
        lambda *_args, **_kwargs: attribution,
    )
    covering = attachment._deep_reactive_envelopes(
        changed_files=("src/api.py",),
        revision=REVISION,
    )

    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    recovery = attachment._deep_reactive_envelopes(
        changed_files=(),
        revision=REVISION,
    )
    return _canonicalize_runtime(
        (*syntax, *covering, *recovery)
    )


def _submit_record() -> rr.EvidenceRecord:
    covering = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/test_api.py"],
        "ran": ["tests/test_api.py"],
        "command": ["pytest", "-q", "tests/test_api.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_get_user"],
    }
    verdict = safe_gate_verdict(covering=covering)
    certificate = safe_build_certificate(
        head=verdict,
        submit_revision=REVISION.repository_content,
        covering=covering,
    )
    context = ProducerContext(
        subject="src/api.py",
        provenance=(("src/api.py", 1),),
        revision=REVISION,
        decision_id="COMPLETION",
        causal_neighborhood=(
            "subject:src/api.py",
            "decision:COMPLETION",
        ),
    )
    envelopes = tuple(
        produce_submit_refusal(
            context=context,
            verdict=verdict,
            certificate=certificate,
            output_owner=owner,
        )
        for owner in (
            SubmitEvidenceOwner.REFUSAL,
            SubmitEvidenceOwner.CERTIFICATE,
        )
    )
    assert all(envelope is not None for envelope in envelopes)
    records = rr.canonicalize_evidence_envelopes(envelopes)
    assert len(records) == 1
    return records[0]


@pytest.fixture
def natural_matrix_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, rr.EvidenceRecord]:
    records = [
        *_brief_records(tmp_path / "brief"),
        *_gateway_records(tmp_path / "gateway", monkeypatch),
        _newfile_record(tmp_path / "newfile", monkeypatch),
        *_deep_records(monkeypatch),
        _submit_record(),
    ]
    return {record.feature_id: record for record in records}


class _Model:
    def _prepare_messages_for_api(self, messages):
        return messages

    def _query(self, messages, **kwargs):
        raise AssertionError("matrix verifies staging, not paid/provider dispatch")

    def _gt_exact_provider_payload(self, messages, kwargs):
        return {"model": "fixture", "messages": messages, **kwargs}


class _Agent:
    def add_messages(self, *messages):
        return list(messages)


def _release_and_stage(
    root: Path,
    record: rr.EvidenceRecord,
) -> tuple[rr.EvidenceRecord, MiniSweProviderBoundary]:
    root.mkdir(parents=True, exist_ok=True)
    journal = rr.RuntimeJournal(root / "runtime.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id=f"attempt-{record.feature_id}",
        journal=journal,
        initial_revision=REVISION,
    )
    runtime.ingest_evidence(record)
    decision = rr.ActiveDecision(
        decision_id=record.decision_context.value,
        context=record.decision_context,
        primary_claim=f"Serve the open {record.decision_context.value} decision.",
        required_roles=record.roles,
        useful_roles=(),
        causal_neighborhood=record.causal_neighborhood,
        token_budget=350,
        current_revision=REVISION,
    )
    plan = runtime.prepare_next_inference(
        decisions=(decision,),
        satisfied_predicates=READY_PREDICATES,
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=(
            "graph",
            "lsp",
            "ast_references",
            "exact_lexical_references",
            "structured_test_result",
            "native_test_result",
            "exact_test_execution",
            "ast",
            "exact_lexical_definitions",
            "repository_paths",
            "fts5",
            "history",
            "sibling_structure",
            "build_metadata",
            "issue_text",
            "obligation_parser",
            "canonical_event_history",
            "test_fingerprint",
            "parser_result",
            "compiler_result",
            "canonical_validation_state",
        ),
        native_observation="native observation",
        observation_id=f"obs-{record.feature_id}",
        source_model_call_id="source-call",
        model_call_id=f"model-{record.feature_id}",
    )
    assert plan.delivery_attempt_id
    boundary = MiniSweProviderBoundary(
        model=_Model(),
        agent=_Agent(),
        attempt_runtime=runtime,
    )
    boundary.stage(
        plan.compilation,
        delivery_attempt_id=plan.delivery_attempt_id,
    )
    released = runtime.evidence_record(record.evidence_id)
    assert released is not None
    return released, boundary


def test_authoritative_exact_10_fact_plus_7_owner_roster() -> None:
    assert FACT_IDS == frozenset(
        {
            "caller_contract",
            "covering_red",
            "def_partition",
            "localization",
            "newfile_precedent",
            "obligations",
            "recovery",
            "signature_delta",
            "submit_refusal",
            "syntax_result",
        }
    )
    assert feature_lineage.CAP_BYTE_OWNER_IDS == frozenset(OWNER_TO_FACT)
    assert {
        owner: next(
            binding.fact_class
            for binding in mechanism.bindings
            if binding.fact_class is not None
        )
        for owner, mechanism in (
            feature_lineage.CAP_BYTE_OWNER_MECHANISMS.items()
        )
    } == OWNER_TO_FACT


def test_natural_boundaries_produce_all_ten_canonical_fact_records(
    natural_matrix_records: dict[str, rr.EvidenceRecord],
) -> None:
    assert set(natural_matrix_records) == FACT_IDS
    assert all(record.provenance for record in natural_matrix_records.values())
    assert all(record.claim for record in natural_matrix_records.values())
    assert all(
        record.actionable_consequence
        for record in natural_matrix_records.values()
    )


def test_origin_assurance_does_not_overclaim_stubbed_substrates() -> None:
    assert set(ORIGIN_ASSURANCE) == FACT_IDS
    assert ORIGIN_ASSURANCE["localization"] == (
        "structured_gateway_stubbed_localizer"
    )
    assert {
        fact_class
        for fact_class, assurance in ORIGIN_ASSURANCE.items()
        if assurance == "deep_hook_stubbed_engine_result"
    } == {"covering_red", "recovery", "syntax_result"}
    assert ORIGIN_ASSURANCE["submit_refusal"] == (
        "structured_submit_host_boundary"
    )


def test_all_seven_cap_owners_are_lineage_on_the_same_physical_facts(
    natural_matrix_records: dict[str, rr.EvidenceRecord],
) -> None:
    actual = {
        owner: record.feature_id
        for record in natural_matrix_records.values()
        for owner in record.owner_feature_ids
    }
    assert actual == OWNER_TO_FACT
    submit = natural_matrix_records["submit_refusal"]
    assert submit.owner_feature_ids == (
        "GT_CERT_DELIVERY",
        "GT_SS_SUBMIT_RED",
    )
    assert len(
        [
            record
            for record in natural_matrix_records.values()
            if record.feature_id == "submit_refusal"
        ]
    ) == 1


@pytest.mark.parametrize("fact_class", sorted(FACT_IDS))
def test_each_natural_fact_ingests_releases_and_occupies_one_provider_slot(
    tmp_path: Path,
    natural_matrix_records: dict[str, rr.EvidenceRecord],
    fact_class: str,
) -> None:
    released, boundary = _release_and_stage(
        tmp_path / f"stage-{fact_class}",
        natural_matrix_records[fact_class],
    )
    assert released.lifecycle is rr.EvidenceLifecycle.RELEASED
    assert boundary.has_unconsumed_capsule is True
    assert boundary._active is not None
    assert boundary._active.delivery_attempt is not None
    assert (
        boundary._active.delivery_attempt.state
        is rr.DeliveryState.COMPILED
    )
    boundary.attempt_runtime.journal.close()


def test_all_deep_capability_kill_switches_are_quiet_before_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_state = SimpleNamespace(
        phase=rr.Phase.RECOVERY,
        focused_symbols=("get_user",),
        focused_files=("src/api.py",),
        edited_files=("src/api.py",),
        current_failures=("same-failure",),
        transition_rules=("repeated_failure_after_edit",),
    )
    attachment = seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=SimpleNamespace(work_state=work_state),
        provider_boundary=None,
        gateway_state=None,
        graph_revision=REVISION.graph,
    )
    for flag in (
        "GT_EDIT_CHECK",
        "GT_VERIFY_EXECUTE",
        "GT_HYPOTHESIS",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled syntax producer executed")
        ),
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.select_covering_tests",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled covering producer executed")
        ),
    )

    assert attachment._deep_reactive_envelopes(
        changed_files=("src/api.py",),
        revision=REVISION,
    ) == ()


def test_gateway_capability_kill_switches_remove_only_their_owned_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _graph(tmp_path)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    monkeypatch.setenv("GT_CHANGE_SURFACE", "0")
    state = gateway.GatewayState(
        graph_db=str(db),
        repo_root=str(tmp_path),
        graph_revision=REVISION.graph,
    )
    state.canonical_revision = REVISION
    event = gateway.ToolEvent(
        kind=gateway.KIND_EDIT,
        carrier_kind=gateway.KIND_EDIT,
        command="structured edit",
        action_index=1,
        changed_files=("src/api.py",),
        edit_before_after={
            "src/api.py": (
                "def get_user(uid):\n    return uid\n",
                "def get_user(uid, name):\n    return uid\n",
            )
        },
        semantic_events=("edit_result",),
        primary_boundary="edit_result",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )

    records = rr.canonicalize_evidence_envelopes(
        gateway.produce_raw(event, state)
    )

    # The unowned caller-contract FACT remains available.  The disabled
    # GT_PATCH_DELTA owner and its signature-delta FACT are absent.
    assert {record.feature_id for record in records} == {"caller_contract"}
    assert all(not record.owner_feature_ids for record in records)

    monkeypatch.setattr(
        gateway,
        "_compute_ranked_localization_rows",
        lambda _state: (_ for _ in ()).throw(
            AssertionError("disabled localization producer executed")
        ),
    )
    search = gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        carrier_kind=gateway.KIND_SEARCH,
        command="rg get_user",
        output="src/api.py:1:get_user\nsrc/alt.py:1:get_user",
        action_index=2,
        semantic_events=("search_result",),
        primary_boundary="search_result",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )
    search_records = rr.canonicalize_evidence_envelopes(
        gateway.produce_raw(search, state)
    )
    assert {record.feature_id for record in search_records} == {
        "def_partition"
    }

    monkeypatch.setattr(
        gateway,
        "detect_change_surface",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled change-surface producer executed")
        ),
    )
    for action_index in (3, 4):
        absent = gateway.ToolEvent(
            kind=gateway.KIND_SEARCH,
            carrier_kind=gateway.KIND_SEARCH,
            command="rg azure",
            output="",
            exit_status=1,
            action_index=action_index,
            semantic_events=("search_result", "failed_search"),
            primary_boundary="failed_search",
            state_revision=REVISION.runtime_evidence,
            semantics_authoritative=True,
        )
        assert all(
            envelope.evidence_type
            not in {"new_file_destination", "missing_role"}
            for envelope in gateway.produce_raw(absent, state)
        )


def test_submit_red_without_verify_execute_preserves_native_unowned_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Agent(_Agent):
        def __init__(self) -> None:
            self.executed = 0

        def execute_actions(self, _message):
            self.executed += 1
            return [{"output": "native submit", "returncode": 0}]

    agent = Agent()
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.delenv("GT_CERT_DELIVERY", raising=False)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=agent,
        env={
            "GT_ATTEMPT_ID": "attempt-submit-flags-off",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "missing-brief.txt"),
        },
        task="native issue",
    )
    attachment.last_covering_result = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/test_api.py"],
        "ran": ["tests/test_api.py"],
        "command": ["pytest", "-q", "tests/test_api.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_get_user"],
    }

    result = agent.execute_actions(
        {
            "extra": {
                "response": {"id": "provider-submit-off"},
                "actions": [
                    {"operation": "SUBMIT", "command": "submit"},
                ],
            }
        }
    )

    assert result == [{"output": "native submit", "returncode": 0}]
    assert agent.executed == 1
    assert all(
        record.feature_id != "submit_refusal"
        for record in attachment.attempt_runtime._evidence.values()
    )
    assert attachment.provider_boundary.has_unconsumed_capsule is False
    attachment.attempt_runtime.journal.close()


def test_structured_submit_boundary_merges_both_owners_and_stages_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Agent(_Agent):
        def __init__(self) -> None:
            self.executed = 0

        def execute_actions(self, _message):
            self.executed += 1
            return [{"output": "native submit", "returncode": 0}]

    source = tmp_path / "src" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def get_user(uid):\n    return uid\n",
        encoding="utf-8",
    )
    agent = Agent()
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=agent,
        env={
            "GT_ATTEMPT_ID": "attempt-submit-natural",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "missing-brief.txt"),
        },
        task="repair get_user",
    )
    view = {"command": "view", "path": "src/api.py"}
    attachment.observe_action_proposal(view)
    attachment.observe_action_result(
        view,
        {"output": source.read_text(encoding="utf-8"), "returncode": 0},
    )
    attachment.last_covering_result = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/test_api.py"],
        "ran": ["tests/test_api.py"],
        "command": ["pytest", "-q", "tests/test_api.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_get_user"],
    }

    result = agent.execute_actions(
        {
            "extra": {
                "response": {"id": "provider-submit-natural"},
                "actions": [
                    {"operation": "SUBMIT", "command": "submit"},
                ],
            }
        }
    )

    assert result == []
    assert agent.executed == 0
    submit_records = tuple(
        record
        for record in attachment.attempt_runtime._evidence.values()
        if record.feature_id == "submit_refusal"
    )
    assert len(submit_records) == 1
    assert submit_records[0].owner_feature_ids == (
        "GT_CERT_DELIVERY",
        "GT_SS_SUBMIT_RED",
    )
    assert submit_records[0].lifecycle is rr.EvidenceLifecycle.RELEASED
    assert attachment.provider_boundary.has_unconsumed_capsule is True
    attachment.attempt_runtime.journal.close()
