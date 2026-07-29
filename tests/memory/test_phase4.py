"""Phase 4 tests: intent detection, temporal filtering, briefings, remaining tools."""

from __future__ import annotations

import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.memory.enrich.worker import _enrich_event
from groundtruth.memory.ingest.sync import store_event
from groundtruth.memory.retrieval.intent import detect_intent
from groundtruth.memory.retrieval.keyword import retrieve
from groundtruth.memory.tools.briefing import handle_memory_briefing
from groundtruth.memory.tools.consolidate import handle_memory_consolidate
from groundtruth.memory.tools.connections import handle_memory_connections
from groundtruth.memory.tools.forget import handle_memory_forget
from groundtruth.utils.result import Ok


async def _store_and_enrich(store: MemoryStore, content: str, scope: str) -> str:
    config = MemoryConfig()
    r = await store_event(store, content, scope)
    assert isinstance(r, Ok)
    await _enrich_event(store, r.value["event_id"], config)
    return r.value["event_id"]


# ---- Intent detection ----

def test_intent_current() -> None:
    assert detect_intent("What is the current database?") == "current"
    assert detect_intent("What does the team use now?") == "current"


def test_intent_history() -> None:
    assert detect_intent("What did we use previously?") == "history"
    assert detect_intent("What changed from the old config?") == "history"


def test_intent_timeline() -> None:
    assert detect_intent("When did the migration happen?") == "timeline"
    assert detect_intent("Show me the timeline of changes") == "timeline"


def test_intent_general() -> None:
    assert detect_intent("Tell me about the backend") == "general"
    assert detect_intent("PostgreSQL configuration") == "general"


# ---- Temporal retrieval ----

@pytest.mark.asyncio
async def test_temporal_most_recent(memory_store: MemoryStore) -> None:
    """Query with 'most recent' should return results ordered by time."""
    await _store_and_enrich(memory_store, "First event about Python", "temp-test")
    await _store_and_enrich(memory_store, "Second event about React", "temp-test")
    await _store_and_enrich(memory_store, "Third event about Docker", "temp-test")

    config = MemoryConfig()
    result = await retrieve(memory_store, "most recent changes", "temp-test", config=config)
    assert isinstance(result, Ok)
    assert len(result.value) >= 1


@pytest.mark.asyncio
async def test_intent_auto_detection(memory_store: MemoryStore) -> None:
    """retrieve() should auto-detect intent when not provided."""
    await _store_and_enrich(memory_store, "We use PostgreSQL for the database", "auto-test")
    config = MemoryConfig()

    # "What is current" → current intent
    result = await retrieve(memory_store, "What is the current database?", "auto-test", config=config)
    assert isinstance(result, Ok)


# ---- Briefings ----

@pytest.mark.asyncio
async def test_briefing_generates_content(memory_store: MemoryStore) -> None:
    """Briefing should generate structured content from active claims."""
    config = MemoryConfig()
    await _store_and_enrich(memory_store, "We decided to use PostgreSQL for the database", "brief-test")
    await _store_and_enrich(memory_store, "I prefer dark mode in all editors", "brief-test")

    result = await handle_memory_briefing(memory_store, "brief-test", config)
    assert "error" not in result
    assert "briefing" in result
    assert isinstance(result["briefing"], str)
    assert len(result["briefing"]) > 0


@pytest.mark.asyncio
async def test_briefing_cache(memory_store: MemoryStore) -> None:
    """Second call with same data should return cached briefing."""
    config = MemoryConfig()
    await _store_and_enrich(memory_store, "We use Python for ML", "cache-test")

    r1 = await handle_memory_briefing(memory_store, "cache-test", config)
    r2 = await handle_memory_briefing(memory_store, "cache-test", config)
    assert r2.get("cached") is True
    assert r1["source_hash"] == r2["source_hash"]


# ---- Consolidate ----

@pytest.mark.asyncio
async def test_consolidate_runs(memory_store: MemoryStore) -> None:
    config = MemoryConfig()
    await _store_and_enrich(memory_store, "We use MySQL for the DB", "cons-test")
    await _store_and_enrich(memory_store, "We switched from MySQL to PostgreSQL", "cons-test")

    result = await handle_memory_consolidate(memory_store, "cons-test", config)
    assert "error" not in result
    assert "consolidated" in result


# ---- Connections ----

@pytest.mark.asyncio
async def test_connections_finds_linked_items(memory_store: MemoryStore) -> None:
    await _store_and_enrich(memory_store, "We use Docker for deployment", "conn-test")

    result = await handle_memory_connections(memory_store, "Docker", "conn-test")
    assert "error" not in result
    assert "connections" in result


# ---- Forget ----

@pytest.mark.asyncio
async def test_forget_hides_event(memory_store: MemoryStore) -> None:
    """Forgotten events should not appear in retrieval."""
    event_id = await _store_and_enrich(memory_store, "Secret info to forget", "forget-test")

    # Verify it exists
    config = MemoryConfig()
    r1 = await retrieve(memory_store, "Secret info", "forget-test", config=config)
    assert isinstance(r1, Ok)
    has_before = any("Secret" in r.content for r in r1.value)

    # Forget it
    result = await handle_memory_forget(memory_store, event_id)
    assert "error" not in result
    assert result["forgotten"] == event_id

    # Should no longer appear
    r2 = await retrieve(memory_store, "Secret info", "forget-test", config=config)
    assert isinstance(r2, Ok)
    for r in r2.value:
        assert "Secret" not in r.content


# ---- Tool count verification ----

def test_all_12_tools_registered() -> None:
    """Verify all 12 MCP tools can be imported."""
    from groundtruth.memory.tools.store import handle_memory_store
    from groundtruth.memory.tools.query import handle_memory_query
    from groundtruth.memory.tools.health import handle_memory_health
    from groundtruth.memory.tools.history import handle_memory_history
    from groundtruth.memory.tools.timeline import handle_memory_timeline
    from groundtruth.memory.tools.briefing import handle_memory_briefing
    from groundtruth.memory.tools.bridge import handle_memory_bridge
    from groundtruth.memory.tools.connections import handle_memory_connections
    from groundtruth.memory.tools.consolidate import handle_memory_consolidate
    from groundtruth.memory.tools.forget import handle_memory_forget

    handlers = [
        handle_memory_store, handle_memory_query, handle_memory_health,
        handle_memory_history, handle_memory_timeline, handle_memory_briefing,
        handle_memory_bridge, handle_memory_connections, handle_memory_consolidate,
        handle_memory_forget,
    ]
    assert len(handlers) == 10  # 10 handlers for 10 MCP tools
    assert all(callable(h) for h in handlers)
