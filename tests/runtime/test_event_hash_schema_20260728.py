"""Schema-versioned verification for persisted canonical-event hashes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from groundtruth.runtime import reasoning_runtime as rr


def _revision() -> rr.RevisionVector:
    return rr.RevisionVector(
        repository_content="repo-1",
        graph="graph-1",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )


def _action_result() -> rr.CanonicalEvent:
    revision = _revision()
    action = rr.CanonicalAction(
        action_id="action-1",
        operation=rr.ActionOperation.VIEW_SOURCE,
        tool_family="shell",
        tool_name="mini-swe",
        structured_operation="view",
        subject="src/pkg/mod.py",
        targets=("src/pkg/mod.py",),
        raw_command="cat src/pkg/mod.py",
    )
    return rr.CanonicalEvent(
        event_id="event-1",
        attempt_id="attempt-1",
        sequence=1,
        kind=rr.EventKind.ACTION_RESULT,
        authority=rr.Authority.STRUCTURED,
        outcomes=(),
        revision_before=revision,
        revision_after=revision,
        previous_event_hash="",
        action_id=action.action_id,
        action=action,
        result=rr.CanonicalResult(
            status="success",
            files_hit=("src/pkg/mod.py",),
            viewed_symbols=("alpha",),
            resolved_symbols=("beta",),
        ),
    )


def _old_schema_payload(event: rr.CanonicalEvent) -> str:
    raw = json.loads(event.canonical_json())
    del raw["result"]["viewed_symbols"]
    del raw["result"]["resolved_symbols"]
    return json.dumps(
        raw,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _create_pre_marker_journal(
    path,
    *,
    payload: str,
    content_hash: str,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE canonical_events (
                event_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                previous_event_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                UNIQUE(attempt_id, sequence)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO canonical_events(
                event_id, attempt_id, sequence, previous_event_hash,
                content_hash, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "event-1",
                "attempt-1",
                1,
                "",
                content_hash,
                payload,
            ),
        )


def _create_versioned_journal(
    path,
    *,
    payload: str,
    content_hash: str,
    hash_schema: str,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE canonical_events (
                event_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                previous_event_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                hash_schema TEXT NOT NULL DEFAULT '',
                UNIQUE(attempt_id, sequence)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO canonical_events(
                event_id, attempt_id, sequence, previous_event_hash,
                content_hash, canonical_json, hash_schema
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-1",
                "attempt-1",
                1,
                "",
                content_hash,
                payload,
                hash_schema,
            ),
        )


def test_pre_marker_action_result_is_migrated_and_remains_readable(tmp_path):
    db = tmp_path / "runtime.sqlite3"
    payload = _old_schema_payload(_action_result())
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    _create_pre_marker_journal(
        db,
        payload=payload,
        content_hash=content_hash,
    )

    with rr.RuntimeJournal(db) as journal:
        columns = {
            row[1]: row for row in journal.connection.execute(
                "PRAGMA table_info(canonical_events)"
            )
        }
        assert columns["hash_schema"][3] == 1
        assert columns["hash_schema"][4] == "''"
        assert journal.connection.execute(
            "SELECT hash_schema FROM canonical_events"
        ).fetchone() == ("",)

        restored = journal.events("attempt-1")

    assert len(restored) == 1
    assert restored[0].result == rr.CanonicalResult(
        status="success",
        files_hit=("src/pkg/mod.py",),
    )


def test_new_rows_are_current_schema_without_changing_event_bytes(tmp_path):
    event = _action_result()
    canonical_json_before = event.canonical_json()
    content_hash_before = event.content_hash

    with rr.RuntimeJournal(tmp_path / "runtime.sqlite3") as journal:
        journal.append(event)
        row = journal.connection.execute(
            """
            SELECT canonical_json, content_hash, hash_schema
            FROM canonical_events
            """
        ).fetchone()
        assert row == (
            canonical_json_before,
            content_hash_before,
            rr.CANONICAL_HASH_SCHEMA,
        )
        assert journal.events("attempt-1") == (event,)

    assert event.canonical_json() == canonical_json_before
    assert event.content_hash == content_hash_before


def test_reappended_historical_event_keeps_legacy_schema_identity(tmp_path):
    source_db = tmp_path / "source.sqlite3"
    payload = _old_schema_payload(_action_result())
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    _create_pre_marker_journal(
        source_db,
        payload=payload,
        content_hash=content_hash,
    )

    with rr.RuntimeJournal(source_db) as source:
        historical = source.events("attempt-1")[0]

    with rr.RuntimeJournal(tmp_path / "destination.sqlite3") as destination:
        destination.append(historical)
        stored = destination.connection.execute(
            """
            SELECT canonical_json, content_hash, hash_schema
            FROM canonical_events
            """
        ).fetchone()
        restored = destination.events("attempt-1")

    assert stored == (payload, content_hash, "")
    assert restored == (historical,)
    assert restored[0].content_hash == content_hash


def test_pre_marker_multi_event_chain_preserves_historical_parent_hashes(tmp_path):
    db = tmp_path / "runtime.sqlite3"
    first = _action_result()
    first_payload = _old_schema_payload(first)
    first_hash = hashlib.sha256(first_payload.encode("utf-8")).hexdigest()
    second = replace(
        first,
        event_id="event-2",
        sequence=2,
        previous_event_hash=first_hash,
    )
    second_payload = _old_schema_payload(second)
    second_hash = hashlib.sha256(second_payload.encode("utf-8")).hexdigest()
    _create_pre_marker_journal(
        db,
        payload=first_payload,
        content_hash=first_hash,
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            INSERT INTO canonical_events(
                event_id, attempt_id, sequence, previous_event_hash,
                content_hash, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "event-2",
                "attempt-1",
                2,
                first_hash,
                second_hash,
                second_payload,
            ),
        )

    with rr.RuntimeJournal(db) as journal:
        restored = journal.events("attempt-1")
        tail = journal.events("attempt-1", after_sequence=1)

    assert tuple(event.event_id for event in restored) == ("event-1", "event-2")
    assert tuple(event.content_hash for event in restored) == (first_hash, second_hash)
    assert tail == (restored[1],)
    assert tail[0].previous_event_hash == restored[0].content_hash


def test_tail_read_verifies_requested_parent_schema(tmp_path):
    db = tmp_path / "runtime.sqlite3"
    event = _action_result()
    _create_versioned_journal(
        db,
        payload=event.canonical_json(),
        content_hash=event.content_hash,
        hash_schema="unknown-parent-schema",
    )

    with rr.RuntimeJournal(db) as journal:
        with pytest.raises(
            rr.EventSchemaVersionError,
            match="unknown-parent-schema",
        ):
            journal.events("attempt-1", after_sequence=1)


def test_unknown_hash_schema_has_a_distinct_version_error(tmp_path):
    event = _action_result()
    db = tmp_path / "runtime.sqlite3"
    _create_versioned_journal(
        db,
        payload=event.canonical_json(),
        content_hash=event.content_hash,
        hash_schema="999",
    )

    with rr.RuntimeJournal(db) as journal:
        with pytest.raises(
            rr.EventSchemaVersionError,
            match="hash schema.*999",
        ):
            journal.events("attempt-1")


def test_old_payload_falsely_labeled_current_is_a_schema_error(tmp_path):
    db = tmp_path / "runtime.sqlite3"
    payload = _old_schema_payload(_action_result())
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    _create_versioned_journal(
        db,
        payload=payload,
        content_hash=content_hash,
        hash_schema=rr.CANONICAL_HASH_SCHEMA,
    )

    with rr.RuntimeJournal(db) as journal:
        with pytest.raises(
            rr.EventSchemaVersionError,
            match="schema.*does not round-trip",
        ):
            journal.events("attempt-1")


def test_raw_corruption_is_not_hidden_by_an_unknown_schema_label(tmp_path):
    event = _action_result()
    db = tmp_path / "runtime.sqlite3"
    _create_versioned_journal(
        db,
        payload=event.canonical_json() + " ",
        content_hash=event.content_hash,
        hash_schema="unknown",
    )

    with rr.RuntimeJournal(db) as journal:
        with pytest.raises(rr.EventIntegrityError, match="content hash|tamper") as raised:
            journal.events("attempt-1")

    assert type(raised.value) is rr.EventIntegrityError
