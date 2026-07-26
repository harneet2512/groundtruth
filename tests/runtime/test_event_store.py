"""RED integration contract for the append-only SQLite canonical event store."""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime.reasoning_runtime import (
    Authority,
    CanonicalEvent,
    EventIntegrityError,
    EventKind,
    EventStore,
    Phase,
    RevisionVector,
    SemanticKind,
    SemanticOutcome,
    StateIntegrityError,
    WorkState,
    reduce_event,
)


def _revision(repo: str) -> RevisionVector:
    return RevisionVector(
        repository_content=repo,
        graph="graph-1",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )


def _event(
    sequence: int,
    *,
    previous_hash: str = "",
    event_id: str | None = None,
    before: RevisionVector | None = None,
    after: RevisionVector | None = None,
    outcome: SemanticOutcome | None = None,
) -> CanonicalEvent:
    before = before or _revision("repo-1")
    return CanonicalEvent(
        event_id=event_id or f"ev-{sequence}",
        attempt_id="attempt-1",
        sequence=sequence,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=Authority.RESULT_DERIVED,
        outcomes=(() if outcome is None else (outcome,)),
        revision_before=before,
        revision_after=after or before,
        previous_event_hash=previous_hash,
    )


def test_store_uses_wal_and_reads_events_in_canonical_sequence(tmp_path):
    db = tmp_path / "events.sqlite3"
    with EventStore(db) as store:
        first = _event(1)
        second = _event(2, previous_hash=first.content_hash)
        store.append(first)
        store.append(second)
        assert store.events("attempt-1") == (first, second)

    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_duplicate_id_gap_and_wrong_parent_hash_are_rejected(tmp_path):
    with EventStore(tmp_path / "events.sqlite3") as store:
        first = _event(1)
        store.append(first)

        with pytest.raises(EventIntegrityError, match="duplicate"):
            store.append(_event(2, previous_hash=first.content_hash, event_id=first.event_id))
        with pytest.raises(EventIntegrityError, match="sequence|gap"):
            store.append(_event(3, previous_hash=first.content_hash))
        with pytest.raises(EventIntegrityError, match="parent|hash"):
            store.append(_event(2, previous_hash="not-the-parent"))

        assert store.events("attempt-1") == (first,)


def test_batch_append_is_atomic_when_a_later_event_is_invalid(tmp_path):
    with EventStore(tmp_path / "events.sqlite3") as store:
        first = _event(1)
        invalid_second = _event(3, previous_hash=first.content_hash)

        with pytest.raises(EventIntegrityError):
            store.append_batch((first, invalid_second))

        assert store.events("attempt-1") == ()


def test_verified_snapshot_loads_with_only_its_committed_tail(tmp_path):
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    first = _event(
        1,
        before=rev1,
        after=rev2,
        outcome=SemanticOutcome(
            kind=SemanticKind.EDIT_EXECUTED,
            subject="src/auth.py",
            changed=True,
        ),
    )
    second = _event(
        2,
        previous_hash=first.content_hash,
        before=rev2,
        outcome=SemanticOutcome(
            kind=SemanticKind.TEST_FAIL,
            failure_fingerprint="red-1",
        ),
    )
    third = _event(
        3,
        previous_hash=second.content_hash,
        before=rev2,
        outcome=SemanticOutcome(kind=SemanticKind.SOURCE_VIEWED, subject="src/other.py"),
    )
    state = WorkState.initial(attempt_id="attempt-1", revision=rev1)
    state = reduce_event(state, first)
    state = reduce_event(state, second)
    assert state.phase is Phase.RECOVERY
    assert "failure_after_edit" in state.transition_rules

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.append_batch((first, second))
        store.save_snapshot(state)
        store.append(third)

        snapshot, tail = store.load_snapshot_and_tail("attempt-1")

    assert snapshot == state
    assert snapshot.sequence == 2
    assert tail == (third,)


def test_event_read_rejects_tampered_canonical_payload(tmp_path):
    db = tmp_path / "events.sqlite3"
    with EventStore(db) as store:
        store.append(_event(1))
        store.connection.execute(
            """
            UPDATE canonical_events
            SET canonical_json = replace(canonical_json, 'ev-1', 'ev-X')
            WHERE event_id = 'ev-1'
            """
        )
        store.connection.commit()

        with pytest.raises(EventIntegrityError, match="content hash|tamper"):
            store.events("attempt-1")


def test_snapshot_save_rejects_state_not_derived_from_committed_events(tmp_path):
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    first = _event(
        1,
        before=rev1,
        after=rev2,
        outcome=SemanticOutcome(
            kind=SemanticKind.EDIT_EXECUTED,
            subject="src/auth.py",
            changed=True,
        ),
    )
    wrong_state = WorkState(
        attempt_id="attempt-1",
        revision=rev2,
        sequence=1,
        phase=Phase.COMPLETION,
        edited_files=("src/not-auth.py",),
    )

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.append(first)
        with pytest.raises(StateIntegrityError, match="replay|derived|state hash"):
            store.save_snapshot(wrong_state)


def test_snapshot_load_revalidates_the_snapshot_bound_event_payload(tmp_path):
    rev1, rev2 = _revision("repo-1"), _revision("repo-2")
    event = _event(
        1,
        before=rev1,
        after=rev2,
        outcome=SemanticOutcome(
            kind=SemanticKind.EDIT_EXECUTED,
            subject="src/auth.py",
            changed=True,
        ),
    )
    state = reduce_event(
        WorkState.initial(attempt_id="attempt-1", revision=rev1),
        event,
    )

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.append(event)
        store.save_snapshot(state)
        store.connection.execute(
            """
            UPDATE canonical_events
            SET canonical_json = replace(canonical_json, 'src/auth.py', 'src/other.py')
            WHERE event_id = 'ev-1'
            """
        )
        store.connection.commit()

        with pytest.raises(EventIntegrityError, match="content hash|tamper"):
            store.load_snapshot_and_tail("attempt-1")
