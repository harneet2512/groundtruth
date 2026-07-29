"""End-to-end tests for memory MCP tool handlers."""

from __future__ import annotations

import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.memory.tools.health import handle_memory_health
from groundtruth.memory.tools.query import handle_memory_query
from groundtruth.memory.tools.store import handle_memory_store


@pytest.mark.asyncio
async def test_store_tool_returns_event_id(
    memory_store: MemoryStore, memory_config: MemoryConfig
) -> None:
    """groundtruth_memory_store handler should return event_id."""
    result = await handle_memory_store(
        store=memory_store,
        raw_content="User prefers dark mode",
        scope_id="tool-test",
    )
    assert "error" not in result
    assert "event_id" in result
    assert result["status"] == "stored"


@pytest.mark.asyncio
async def test_query_tool_finds_stored_content(
    memory_store: MemoryStore, memory_config: MemoryConfig
) -> None:
    """Store then query — should find the stored content."""
    await handle_memory_store(
        store=memory_store,
        raw_content="The team decided to use PostgreSQL",
        scope_id="tool-test",
    )

    result = await handle_memory_query(
        store=memory_store,
        query="PostgreSQL",
        scope_id="tool-test",
        config=memory_config,
    )
    assert "error" not in result
    assert result["result_count"] >= 1
    assert any("PostgreSQL" in r["content"] for r in result["results"])


@pytest.mark.asyncio
async def test_health_tool_reports_stats(
    memory_store: MemoryStore, memory_config: MemoryConfig
) -> None:
    """groundtruth_memory_health should report accurate counts."""
    # Store some events
    for i in range(5):
        await handle_memory_store(
            store=memory_store,
            raw_content=f"Health test event {i}",
            scope_id="health-test",
        )

    result = await handle_memory_health(
        store=memory_store,
        config=memory_config,
    )
    assert "error" not in result
    assert "health" in result
    assert result["health"]["event_count"] == 5
    assert result["health"]["scope_count"] >= 1
    assert result["health"]["enrichment_pending"] >= 0
    assert "embedding_status" in result["health"]


@pytest.mark.asyncio
async def test_query_scope_isolation_via_tools(
    memory_store: MemoryStore, memory_config: MemoryConfig
) -> None:
    """Tool-level scope isolation: query one scope, don't see the other."""
    await handle_memory_store(
        store=memory_store,
        raw_content="Alpha secret: the password is hunter2",
        scope_id="scope-x",
    )
    await handle_memory_store(
        store=memory_store,
        raw_content="Beta secret: the API key is abc123",
        scope_id="scope-y",
    )

    result_x = await handle_memory_query(
        store=memory_store,
        query="secret",
        scope_id="scope-x",
        config=memory_config,
    )
    assert "error" not in result_x
    for r in result_x["results"]:
        assert "Alpha" in r["content"]
