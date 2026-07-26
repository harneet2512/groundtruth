"""RED contract for the canonical append-only runtime journal.

The in-memory delivery state machine proves that a transition is locally
valid.  These tests pin the stronger persistence invariants needed across
crashes, retries, and deterministic replay:

* one immutable delivery-attempt/model-call identity;
* exactly one provider-terminal outcome for that identity;
* identical terminal replay is idempotent, contradictory replay is corrupt;
* response commitment is a later journal fact, not a terminal rewrite;
* event and delivery journals reject UPDATE/DELETE;
* an unverified/corrupt committed head cannot be extended.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime.reasoning_runtime import (
    Authority,
    CanonicalEvent,
    DeliveryAttempt,
    DeliveryState,
    EventIntegrityError,
    EventKind,
    ModelCallAttempt,
    ProviderTerminalKind,
    RevisionVector,
    RuntimeJournal,
    StateIntegrityError,
    advance_delivery,
    commit_response,
    record_provider_terminal,
)


CAPSULE_HASH = "a" * 64
PAYLOAD_HASH = "b" * 64
RESPONSE_HASH = "c" * 64


def _revision() -> RevisionVector:
    return RevisionVector(
        repository_content="repo-1",
        graph="graph-1",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )


def _event(
    sequence: int,
    *,
    previous_hash: str = "",
    event_id: str | None = None,
) -> CanonicalEvent:
    revision = _revision()
    return CanonicalEvent(
        event_id=event_id or f"ev-{sequence}",
        attempt_id="attempt-1",
        sequence=sequence,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=Authority.RESULT_DERIVED,
        outcomes=(),
        revision_before=revision,
        revision_after=revision,
        previous_event_hash=previous_hash,
    )


def _provider_accepted(
    *,
    model_call_id: str,
    provider_response_id: str,
) -> DeliveryAttempt:
    attempt = DeliveryAttempt(
        evidence_ids=("GT-E144",),
        capsule_hash=CAPSULE_HASH,
        model_call_id=model_call_id,
    )
    attempt = advance_delivery(
        attempt,
        DeliveryState.COMPILED,
        observation_id="obs-205",
    )
    attempt = advance_delivery(
        attempt,
        DeliveryState.JOINED,
        joined_capsule_hash=CAPSULE_HASH,
        provider_payload_hash=PAYLOAD_HASH,
    )
    attempt = advance_delivery(attempt, DeliveryState.DISPATCHED)
    return advance_delivery(
        attempt,
        DeliveryState.PROVIDER_ACCEPTED,
        provider_response_id=provider_response_id,
    )


def _terminal(
    accepted: DeliveryAttempt,
    kind: ProviderTerminalKind,
) -> DeliveryAttempt:
    return record_provider_terminal(
        accepted,
        ModelCallAttempt(
            model_call_id=accepted.model_call_id,
            joined_capsule_hash=accepted.joined_capsule_hash,
            provider_payload_hash=accepted.provider_payload_hash,
            provider_response_id=accepted.provider_response_id,
            terminal_kind=kind,
        ),
    )


def _delivery_prefix(
    accepted: DeliveryAttempt,
) -> tuple[DeliveryAttempt, ...]:
    selected = DeliveryAttempt(
        evidence_ids=accepted.evidence_ids,
        capsule_hash=accepted.capsule_hash,
        model_call_id=accepted.model_call_id,
    )
    compiled = advance_delivery(
        selected,
        DeliveryState.COMPILED,
        observation_id=accepted.observation_id,
    )
    joined = advance_delivery(
        compiled,
        DeliveryState.JOINED,
        joined_capsule_hash=accepted.joined_capsule_hash,
        provider_payload_hash=accepted.provider_payload_hash,
    )
    dispatched = advance_delivery(joined, DeliveryState.DISPATCHED)
    return (selected, compiled, joined, dispatched, accepted)


def _append_prefix(
    journal: RuntimeJournal,
    delivery_attempt_id: str,
    accepted: DeliveryAttempt,
) -> None:
    for state in _delivery_prefix(accepted):
        journal.append_delivery(delivery_attempt_id, state)


def test_runtime_journal_uses_wal_and_blocks_event_and_delivery_rewrites(
    tmp_path,
) -> None:
    db = tmp_path / "runtime.sqlite3"
    event = _event(1)
    accepted = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-12a",
    )

    with RuntimeJournal(db) as journal:
        journal.append(event)
        _append_prefix(journal, "delivery-12a", accepted)

        for statement in (
            "UPDATE canonical_events SET event_id = 'rewritten'",
            "DELETE FROM canonical_events",
            "UPDATE delivery_journal SET model_call_id = 'rewritten'",
            "DELETE FROM delivery_journal",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append.only|immutable"):
                journal.connection.execute(statement)

        assert journal.events("attempt-1") == (event,)
        assert journal.delivery_history("delivery-12a") == _delivery_prefix(
            accepted
        )

    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_identical_terminal_replay_is_idempotent(tmp_path) -> None:
    accepted = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-12a",
    )
    delivered = _terminal(accepted, ProviderTerminalKind.COMPLETED)

    with RuntimeJournal(tmp_path / "runtime.sqlite3") as journal:
        _append_prefix(journal, "delivery-12a", accepted)
        journal.append_delivery("delivery-12a", delivered)
        journal.append_delivery("delivery-12a", delivered)

        assert journal.delivery_history("delivery-12a") == (
            *_delivery_prefix(accepted),
            delivered,
        )


def test_contradictory_terminal_replay_is_a_core_integrity_fault(tmp_path) -> None:
    accepted = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-12a",
    )
    delivered = _terminal(accepted, ProviderTerminalKind.COMPLETED)
    failed = _terminal(accepted, ProviderTerminalKind.FAILED)

    with RuntimeJournal(tmp_path / "runtime.sqlite3") as journal:
        _append_prefix(journal, "delivery-12a", accepted)
        journal.append_delivery("delivery-12a", delivered)

        with pytest.raises(
            StateIntegrityError,
            match="terminal|contradict|integrity",
        ):
            journal.append_delivery("delivery-12a", failed)

        assert journal.delivery_history("delivery-12a") == (
            *_delivery_prefix(accepted),
            delivered,
        )


def test_attempt_and_model_call_identity_are_immutable_across_retries(
    tmp_path,
) -> None:
    first = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-failed",
    )
    first_terminal = _terminal(first, ProviderTerminalKind.FAILED)
    retry = _provider_accepted(
        model_call_id="call-12b",
        provider_response_id="resp-completed",
    )
    retry_terminal = _terminal(retry, ProviderTerminalKind.COMPLETED)

    with RuntimeJournal(tmp_path / "runtime.sqlite3") as journal:
        _append_prefix(journal, "delivery-12a", first)
        journal.append_delivery("delivery-12a", first_terminal)
        _append_prefix(journal, "delivery-12b", retry)
        journal.append_delivery("delivery-12b", retry_terminal)

        assert journal.delivery_history("delivery-12a") == (
            *_delivery_prefix(first),
            first_terminal,
        )
        assert journal.delivery_history("delivery-12b") == (
            *_delivery_prefix(retry),
            retry_terminal,
        )

        with pytest.raises(StateIntegrityError, match="model.call|model_call"):
            journal.append_delivery("delivery-12a", retry)
        with pytest.raises(StateIntegrityError, match="model.call|model_call"):
            journal.append_delivery("delivery-alias", first)


def test_hashes_persist_and_response_commit_is_a_separate_journal_fact(
    tmp_path,
) -> None:
    db = tmp_path / "runtime.sqlite3"
    accepted = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-12a",
    )
    delivered = _terminal(accepted, ProviderTerminalKind.COMPLETED)
    committed = commit_response(delivered, response_hash=RESPONSE_HASH)

    with RuntimeJournal(db) as journal:
        _append_prefix(journal, "delivery-12a", accepted)
        journal.append_delivery("delivery-12a", delivered)
        journal.append_delivery("delivery-12a", committed)

    with RuntimeJournal(db) as journal:
        history = journal.delivery_history("delivery-12a")
        assert tuple(item.state for item in history) == (
            DeliveryState.SELECTED,
            DeliveryState.COMPILED,
            DeliveryState.JOINED,
            DeliveryState.DISPATCHED,
            DeliveryState.PROVIDER_ACCEPTED,
            DeliveryState.DELIVERED,
            DeliveryState.RESPONSE_COMMITTED,
        )
        assert history[5].capsule_hash == CAPSULE_HASH
        assert history[5].provider_payload_hash == PAYLOAD_HASH
        assert history[5].response_hash == ""
        assert history[6].response_hash == RESPONSE_HASH

        persisted = journal.connection.execute(
            """
            SELECT capsule_hash, provider_payload_hash, response_hash
            FROM delivery_journal
            WHERE delivery_attempt_id = ?
            ORDER BY journal_sequence
            """,
            ("delivery-12a",),
        ).fetchall()
        assert persisted[-3:] == [
            (CAPSULE_HASH, PAYLOAD_HASH, ""),
            (CAPSULE_HASH, PAYLOAD_HASH, ""),
            (CAPSULE_HASH, PAYLOAD_HASH, RESPONSE_HASH),
        ]


def test_corrupt_existing_event_head_cannot_be_extended_before_verification(
    tmp_path,
) -> None:
    db = tmp_path / "runtime.sqlite3"
    first = _event(1)
    with RuntimeJournal(db) as journal:
        journal.append(first)

    # Simulate out-of-process storage corruption with a syntactically append-only
    # row whose declared digest does not match its payload.  A head-only sequence
    # check would wrongly accept event 3 after this row.
    fake_hash = "f" * 64
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            INSERT INTO canonical_events(
                event_id, attempt_id, sequence, previous_event_hash,
                content_hash, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "ev-corrupt",
                "attempt-1",
                2,
                first.content_hash,
                fake_hash,
                "{}",
            ),
        )
        connection.commit()

    third = _event(3, previous_hash=fake_hash)
    with pytest.raises(
        EventIntegrityError,
        match="content hash|tamper|integrity|corrupt",
    ):
        with RuntimeJournal(db) as journal:
            journal.append(third)


def test_corrupt_existing_delivery_head_cannot_be_extended(tmp_path) -> None:
    db = tmp_path / "runtime.sqlite3"
    accepted = _provider_accepted(
        model_call_id="call-12a",
        provider_response_id="resp-12a",
    )
    with RuntimeJournal(db) as journal:
        _append_prefix(journal, "delivery-12a", accepted)

    # INSERT remains the only legal SQL operation, so simulate a torn/foreign
    # writer by appending a row whose canonical value and persisted digest differ.
    with sqlite3.connect(db) as connection:
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(delivery_journal)"
            ).fetchall()
        ]
        prior = connection.execute(
            """
            SELECT * FROM delivery_journal
            WHERE delivery_attempt_id = ?
            ORDER BY journal_sequence DESC
            LIMIT 1
            """,
            ("delivery-12a",),
        ).fetchone()
        assert prior is not None
        corrupt = dict(zip(columns, prior))
        corrupt["journal_sequence"] = 6
        corrupt["state_hash"] = "f" * 64
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO delivery_journal({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(corrupt[column] for column in columns),
        )
        connection.commit()

    delivered = _terminal(accepted, ProviderTerminalKind.COMPLETED)
    with pytest.raises(
        StateIntegrityError,
        match="hash|tamper|integrity|corrupt",
    ):
        with RuntimeJournal(db) as journal:
            journal.append_delivery("delivery-12a", delivered)
