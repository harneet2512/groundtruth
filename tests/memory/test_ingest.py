"""Tests for the synchronous ingest path."""

from __future__ import annotations

import pytest

from groundtruth.memory.db.store import MemoryStore
from groundtruth.memory.ingest.sync import store_event
from groundtruth.utils.result import Ok


@pytest.mark.asyncio
async def test_ingest_stores_event(memory_store: MemoryStore) -> None:
    """store_event should persist the event and return event_id."""
    result = await store_event(
        store=memory_store,
        raw_content="I prefer Python over JavaScript",
        scope_id="project-alpha",
    )
    assert isinstance(result, Ok)
    assert "event_id" in result.value
    assert result.value["status"] == "stored"

    # Verify event exists in database
    event_result = memory_store.get_event(result.value["event_id"])
    assert isinstance(event_result, Ok)
    assert event_result.value is not None
    assert event_result.value.raw_content == "I prefer Python over JavaScript"
    assert event_result.value.scope_id == "project-alpha"


@pytest.mark.asyncio
async def test_ingest_creates_enrichment_job(memory_store: MemoryStore) -> None:
    """Every ingested event must have a corresponding enrichment job."""
    result = await store_event(
        store=memory_store,
        raw_content="Switched from MySQL to PostgreSQL",
        scope_id="project-beta",
    )
    assert isinstance(result, Ok)

    # Check enrichment_jobs table
    row = memory_store.conn.execute(
        "SELECT * FROM enrichment_jobs WHERE event_id = ?",
        (result.value["event_id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_ingest_creates_scope_automatically(memory_store: MemoryStore) -> None:
    """If scope doesn't exist, ingest should create it."""
    result = await store_event(
        store=memory_store,
        raw_content="Test content",
        scope_id="new-scope",
    )
    assert isinstance(result, Ok)

    scope = memory_store.conn.execute(
        "SELECT * FROM scopes WHERE id = 'new-scope'"
    ).fetchone()
    assert scope is not None


@pytest.mark.asyncio
async def test_ingest_records_timing(memory_store: MemoryStore) -> None:
    """store_event should record a timing metric."""
    await store_event(
        store=memory_store,
        raw_content="Some memory content",
        scope_id="metrics-test",
    )

    metrics = memory_store.conn.execute(
        "SELECT * FROM memory_metrics WHERE operation = 'store_event'"
    ).fetchall()
    assert len(metrics) >= 1
    assert metrics[0]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_ingest_100_events_fast(memory_store: MemoryStore) -> None:
    """100 events should ingest without errors."""
    for i in range(100):
        result = await store_event(
            store=memory_store,
            raw_content=f"Event number {i} with some content about testing",
            scope_id="perf-test",
        )
        assert isinstance(result, Ok)

    # Verify all 100 stored
    count = memory_store.conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE scope_id = 'perf-test'"
    ).fetchone()[0]
    assert count == 100

    # Check latency stats
    stats = memory_store.get_latency_stats("store_event")
    assert isinstance(stats, Ok)
    assert stats.value["count"] == 100
