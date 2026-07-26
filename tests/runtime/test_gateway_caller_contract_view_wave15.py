"""Structured file-view attachment for the canonical caller-contract FACT."""

from __future__ import annotations

import sqlite3

from groundtruth.runtime import fact_registry
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters.miniswe import normalize_event


REVISION = rr.RevisionVector(
    repository_content="repo-view-contract",
    graph="graph-view-contract",
    lsp="lsp-view-contract",
    runtime_evidence="runtime-view-contract",
)


def _repository(tmp_path):
    source = tmp_path / "src" / "api.py"
    caller = tmp_path / "src" / "caller.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def get_user(uid):\n    return uid\n",
        encoding="utf-8",
    )
    caller.write_text(
        "def use():\n    return get_user('1')\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
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
        "(2,'Function','use',NULL,'src/caller.py',1,2,NULL,NULL,0,0,'python',NULL);"
        "INSERT INTO edges VALUES"
        "(11,2,1,'CALLS',2,'src/caller.py','import',0.95,NULL);"
    )
    connection.commit()
    connection.close()
    return db


def _state(tmp_path, db):
    state = gateway.GatewayState(
        graph_db=str(db),
        repo_root=str(tmp_path),
        graph_revision=REVISION.graph,
    )
    state.canonical_revision = REVISION
    return state


def test_structured_view_path_is_event_authority_without_command_regex() -> None:
    event = normalize_event(
        "opaque host operation",
        "native source bytes",
        0,
        4,
        viewed_files=("src/api.py",),
    )

    assert event.viewed_files == ("src/api.py",)
    assert event.semantic_events == ("file_view",)
    assert event.primary_boundary == "file_view"
    assert event.semantics_authoritative is True


def test_view_alias_is_canonical_caller_contract_at_file_view() -> None:
    registration = fact_registry.registration_for("caller_contract_view")

    assert registration is not None
    assert registration.fact_class == "caller_contract"
    assert fact_registry.required_event("caller_contract_view") == "file_view"
    assert fact_registry.producer_matches(
        "caller_contract_view",
        "caller_contract",
    )


def test_structured_view_produces_pre_edit_canonical_caller_contract(
    tmp_path,
    monkeypatch,
) -> None:
    db = _repository(tmp_path)
    monkeypatch.setenv("GT_GATEWAY", "1")
    event = normalize_event(
        "opaque host operation",
        "native source bytes",
        0,
        1,
        viewed_files=("src/api.py",),
    )

    envelopes = gateway.produce_raw(event, _state(tmp_path, db))
    records = rr.canonicalize_evidence_envelopes(envelopes)

    assert len(envelopes) == 1
    assert envelopes[0].evidence_type == "caller_contract_view"
    assert envelopes[0].preferred_event == gateway.KIND_VIEW
    assert envelopes[0].provenance == (("src/caller.py", 2),)
    assert envelopes[0].producer_inputs is not None
    assert len(envelopes[0].producer_inputs.caller_rows) == 1
    assert {record.feature_id for record in records} == {"caller_contract"}
    record = records[0]
    assert record.decision_context is rr.DecisionContext.PATCH_CONSTRUCTION
    assert "subject:src/api.py" in record.causal_neighborhood


def test_view_contract_is_quiet_without_exact_view_path_or_gateway_profile(
    tmp_path,
    monkeypatch,
) -> None:
    db = _repository(tmp_path)
    state = _state(tmp_path, db)
    exact = normalize_event(
        "opaque host operation",
        "native source bytes",
        0,
        1,
        viewed_files=("src/api.py",),
    )
    missing_path = gateway.ToolEvent(
        kind=gateway.KIND_VIEW,
        semantic_events=("file_view",),
        primary_boundary="file_view",
        semantics_authoritative=True,
    )

    monkeypatch.delenv("GT_GATEWAY", raising=False)
    assert gateway.produce_raw(exact, state) == []

    monkeypatch.setenv("GT_GATEWAY", "1")
    assert gateway.produce_raw(missing_path, state) == []

