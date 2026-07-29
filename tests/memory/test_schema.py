"""Tests for memory schema creation and database setup."""

from __future__ import annotations

from groundtruth.memory.db.store import MemoryStore
from groundtruth.utils.result import Ok


def test_initialize_creates_all_tables(memory_store: MemoryStore) -> None:
    """All tables from schema.sql must exist after initialization."""
    expected_tables = [
        "memory_events",
        "memory_items",
        "claims",
        "supersession_edges",
        "entities",
        "unmapped_predicates",
        "entity_aliases",
        "memory_entities",
        "scopes",
        "connections",
        "active_claim_heads",
        "briefings",
        "event_embeddings",
        "enrichment_jobs",
        "memory_metrics",
    ]
    rows = memory_store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {r["name"] for r in rows}

    for table in expected_tables:
        assert table in table_names, f"Missing table: {table}"


def test_fts_virtual_tables_exist(memory_store: MemoryStore) -> None:
    """FTS5 virtual tables must be created."""
    rows = memory_store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_fts%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "memory_events_fts" in names
    assert "memory_items_fts" in names


def test_fts_uses_porter_tokenizer(memory_store: MemoryStore) -> None:
    rows = memory_store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name IN ('memory_events_fts', 'memory_items_fts')"
    ).fetchall()
    ddl = "\n".join(row["sql"] for row in rows if row["sql"])
    assert "porter unicode61" in ddl


def test_wal_mode_active(memory_store: MemoryStore) -> None:
    """WAL journal mode must be active."""
    mode = memory_store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    # In-memory databases use "memory" mode, which is fine for testing.
    # On-disk databases should use "wal".
    assert mode in ("wal", "memory")


def test_foreign_keys_enabled(memory_store: MemoryStore) -> None:
    """Foreign keys must be ON."""
    fk = memory_store.conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_scope_create_and_fetch(memory_store: MemoryStore) -> None:
    """ensure_scope should create a new scope and be idempotent."""
    result1 = memory_store.ensure_scope("test-scope", "Test Scope")
    assert isinstance(result1, Ok)
    assert result1.value == "test-scope"

    # Idempotent
    result2 = memory_store.ensure_scope("test-scope")
    assert isinstance(result2, Ok)
    assert result2.value == "test-scope"

    # Verify in database
    row = memory_store.conn.execute(
        "SELECT * FROM scopes WHERE id = 'test-scope'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "Test Scope"
