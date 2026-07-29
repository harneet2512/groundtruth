"""Tests for the retrieval engine."""

from __future__ import annotations

import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.memory.ingest.sync import store_event
from groundtruth.memory.retrieval.keyword import retrieve
from groundtruth.utils.result import Ok


@pytest.mark.asyncio
async def test_fts_search_finds_matching_content(memory_store: MemoryStore) -> None:
    """FTS search should find events matching the query."""
    await store_event(memory_store, "I prefer Python for data science", "scope-a")
    await store_event(memory_store, "JavaScript is great for web development", "scope-a")
    await store_event(memory_store, "Rust is good for systems programming", "scope-a")

    result = await retrieve(memory_store, "Python data", "scope-a")
    assert isinstance(result, Ok)
    assert len(result.value) >= 1
    assert any("Python" in r.content for r in result.value)


@pytest.mark.asyncio
async def test_scope_isolation(memory_store: MemoryStore) -> None:
    """Queries must not return results from other scopes."""
    await store_event(memory_store, "Secret project Alpha details", "scope-alpha")
    await store_event(memory_store, "Secret project Beta details", "scope-beta")

    # Query scope-alpha — must NOT see scope-beta content
    result_a = await retrieve(memory_store, "Secret project", "scope-alpha")
    assert isinstance(result_a, Ok)
    for r in result_a.value:
        assert r.scope_id == "scope-alpha"

    # Query scope-beta — must NOT see scope-alpha content
    result_b = await retrieve(memory_store, "Secret project", "scope-beta")
    assert isinstance(result_b, Ok)
    for r in result_b.value:
        assert r.scope_id == "scope-beta"


@pytest.mark.asyncio
async def test_empty_scope_returns_empty(memory_store: MemoryStore) -> None:
    """Querying a scope with no events should return empty list."""
    memory_store.ensure_scope("empty-scope")
    result = await retrieve(memory_store, "anything", "empty-scope")
    assert isinstance(result, Ok)
    assert len(result.value) == 0


@pytest.mark.asyncio
async def test_result_budget_enforced(memory_store: MemoryStore) -> None:
    """Results should be capped by result_max_items."""
    for i in range(30):
        await store_event(memory_store, f"Test event {i} about databases", "budget-test")

    config = MemoryConfig(result_max_items=5)
    result = await retrieve(memory_store, "databases", "budget-test", config=config)
    assert isinstance(result, Ok)
    assert len(result.value) <= 5


@pytest.mark.asyncio
async def test_hidden_events_excluded(memory_store: MemoryStore) -> None:
    """Events with hidden=1 should not appear in search results."""
    result = await store_event(memory_store, "This should be hidden", "hide-test")
    assert isinstance(result, Ok)
    event_id = result.value["event_id"]

    # Mark as hidden
    memory_store.conn.execute(
        "UPDATE memory_events SET hidden = 1 WHERE id = ?", (event_id,)
    )
    memory_store.conn.commit()

    search_result = await retrieve(memory_store, "hidden", "hide-test")
    assert isinstance(search_result, Ok)
    assert len(search_result.value) == 0
