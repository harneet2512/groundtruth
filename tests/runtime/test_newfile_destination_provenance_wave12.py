"""Real, identity-neutral provenance for new-file destination evidence.

The change-surface detector may infer a destination from repository structure,
but the canonical runtime must not turn that inference into an evidence record
unless the producer retained an auditable repository witness.  These tests use
the natural detector path (no mocked ``ChangeSurfaceResult``) and pin both
available witness substrates:

* registry code lines that reference the established sibling family;
* graph definition lines for the selected implementation template.

The witness sidecar must not alter the legacy envelope's payload, provenance,
dedup key, rendering, serialization, equality, or flag-off behavior.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from groundtruth.pretask.change_surface import detect_change_surface
from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-wave12",
    graph="graph-wave12",
    lsp="lsp-wave12",
    runtime_evidence="runtime-wave12",
)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _provider_repo(root: Path) -> None:
    _write(
        root,
        "providers/__init__.py",
        "from .aws import AwsProvider\n"
        "from .gcp import GcpProvider\n"
        "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n",
    )
    _write(root, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _write(root, "providers/gcp.py", "class GcpProvider:\n    pass\n")


def _event() -> gateway.ToolEvent:
    return gateway.ToolEvent(
        kind=gateway.KIND_SEARCH,
        carrier_kind=gateway.KIND_SEARCH,
        command="rg azure",
        output="",
        action_index=5,
        semantic_events=("failed_search",),
        primary_boundary="failed_search",
        state_revision=REVISION.runtime_evidence,
        semantics_authoritative=True,
    )


def test_tree_detector_retains_exact_registry_code_line_witnesses(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _provider_repo(tmp_path)

    result = detect_change_surface(
        "Add an azure provider like the aws and gcp providers.",
        str(tmp_path),
        None,
    )

    destination = next(
        row for row in result.destinations if row.entity == "azure"
    )
    assert tuple(
        (row.file, row.line, row.kind, row.identity)
        for row in destination.repository_witnesses
    ) == (
        ("providers/__init__.py", 1, "registration_reference", "aws"),
        ("providers/__init__.py", 2, "registration_reference", "gcp"),
        ("providers/aws.py", 1, "template_lexical_reference", "aws"),
    )


def test_graph_detector_retains_selected_template_definition_witness(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _write(tmp_path, "handlers/json.py", "\n\ndef handle_json():\n    return 1\n")
    _write(tmp_path, "handlers/xml.py", "\n\n\n\ndef handle_xml():\n    return 2\n")
    db = tmp_path / "graph.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE nodes("
        "id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, "
        "start_line INTEGER, is_test INTEGER);"
        "INSERT INTO nodes VALUES"
        "(1,'Function','handle_json','handlers/json.py',3,0),"
        "(2,'Function','handle_xml','handlers/xml.py',5,0);"
    )
    connection.commit()
    connection.close()

    result = detect_change_surface(
        "Add a yaml handler like the json and xml handlers.",
        str(tmp_path),
        str(db),
    )

    destination = next(
        row for row in result.destinations if row.entity == "yaml"
    )
    assert destination.template_file == "handlers/json.py"
    assert tuple(
        (row.file, row.line, row.kind, row.identity)
        for row in destination.repository_witnesses
    ) == (("handlers/json.py", 3, "template_definition", "handle_json"),)


def test_gateway_attaches_witnesses_without_legacy_identity_or_byte_drift(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _provider_repo(tmp_path)
    state = gateway.GatewayState(
        repo_root=str(tmp_path),
        issue_text="Add an azure provider like the aws and gcp providers.",
    )
    state.canonical_revision = REVISION

    envelopes = gateway._produce_change_surface(
        _event(), state, require_repeat=False
    )

    envelope = next(
        row
        for row in envelopes
        if row.evidence_type == "new_file_destination"
    )
    inputs = envelope.producer_inputs
    assert inputs is not None
    assert tuple(
        (row.file, row.line, row.kind, row.identity)
        for row in inputs.repository_witness_rows
    ) == (
        ("providers/__init__.py", 1, "registration_reference", "aws"),
        ("providers/__init__.py", 2, "registration_reference", "gcp"),
        ("providers/aws.py", 1, "template_lexical_reference", "aws"),
    )
    assert all(row.source_state is not None for row in inputs.repository_witness_rows)

    legacy = replace(envelope, producer_inputs=None)
    assert envelope.provenance == ()
    assert envelope == legacy
    assert envelope.dedup_key == legacy.dedup_key
    assert ee.render_bytes(envelope) == ee.render_bytes(legacy)
    assert ee.to_dict(envelope) == ee.to_dict(legacy)

    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.feature_id == "newfile_precedent"
    assert record.owner_feature_ids == ("GT_CHANGE_SURFACE",)
    assert record.provenance == (
        "providers/__init__.py:1",
        "providers/__init__.py:2",
        "providers/aws.py:1",
    )


def test_tampered_identity_neutral_witness_is_canonical_quiet(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _provider_repo(tmp_path)
    state = gateway.GatewayState(
        repo_root=str(tmp_path),
        issue_text="Add an azure provider like the aws and gcp providers.",
    )
    state.canonical_revision = REVISION
    envelope = next(
        row
        for row in gateway._produce_change_surface(
            _event(), state, require_repeat=False
        )
        if row.evidence_type == "new_file_destination"
    )
    inputs = envelope.producer_inputs
    assert inputs is not None
    first = inputs.repository_witness_rows[0]
    assert first.source_state is not None

    crossed_candidate = replace(
        envelope,
        producer_inputs=replace(inputs, candidate_id="crossed-candidate"),
    )
    forged_state = replace(
        first.source_state,
        sha256="0" * 64,
    )
    forged_witness = replace(
        envelope,
        producer_inputs=replace(
            inputs,
            repository_witness_rows=(
                replace(first, source_state=forged_state),
            ),
        ),
    )

    assert rr.canonical_evidence_from_envelope(crossed_candidate) is None
    assert rr.canonical_evidence_from_envelope(forged_witness) is None


def test_unwitnessed_destination_remains_explicitly_canonical_quiet(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _write(tmp_path, "handlers/json.py", "def handle():\n    return 1\n")
    _write(tmp_path, "handlers/xml.py", "def handle():\n    return 2\n")
    state = gateway.GatewayState(
        repo_root=str(tmp_path),
        issue_text="Add a yaml handler like the json and xml handlers.",
    )
    state.canonical_revision = REVISION

    envelope = next(
        row
        for row in gateway._produce_change_surface(
            _event(), state, require_repeat=False
        )
        if row.evidence_type == "new_file_destination"
    )

    assert envelope.producer_inputs is None
    assert envelope.provenance == ()
    assert rr.canonical_evidence_from_envelope(envelope) is None


def test_stale_graph_line_is_rejected_before_sidecar_attachment(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _write(tmp_path, "handlers/json.py", "def handle():\n    return 1\n")
    _write(tmp_path, "handlers/xml.py", "def handle():\n    return 2\n")
    db = tmp_path / "graph.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE nodes("
        "id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, "
        "start_line INTEGER, is_test INTEGER);"
        "INSERT INTO nodes VALUES"
        "(1,'Function','handle_json','handlers/json.py',99,0),"
        "(2,'Function','handle_xml','handlers/xml.py',99,0);"
    )
    connection.commit()
    connection.close()
    state = gateway.GatewayState(
        repo_root=str(tmp_path),
        graph_db=str(db),
        issue_text="Add a yaml handler like the json and xml handlers.",
    )
    state.canonical_revision = REVISION

    envelope = next(
        row
        for row in gateway._produce_change_surface(
            _event(), state, require_repeat=False
        )
        if row.evidence_type == "new_file_destination"
    )

    assert envelope.producer_inputs is None
    assert rr.canonical_evidence_from_envelope(envelope) is None


def test_flag_off_stays_empty_before_repository_work(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GT_CHANGE_SURFACE", raising=False)
    _provider_repo(tmp_path)

    result = detect_change_surface(
        "Add an azure provider like the aws and gcp providers.",
        str(tmp_path),
        None,
    )

    assert result.abstained is True
    assert result.abstain_reason == "flag_disabled"
    assert result.destinations == []
