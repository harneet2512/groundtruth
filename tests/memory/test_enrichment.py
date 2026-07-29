"""Phase 2 tests: entity extraction, claim extraction, full enrichment pipeline."""

from __future__ import annotations

import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.memory.enrich.classify import classify_by_rules, ClassificationResult
from groundtruth.memory.enrich.materialize import materialize_item, materialize_claims, link_entities_to_item
from groundtruth.memory.enrich.ner import extract_entities, extract_entities_regex
from groundtruth.memory.enrich.resolve import resolve_entity, resolve_entities
from groundtruth.memory.enrich.worker import _enrich_event
from groundtruth.memory.ingest.sync import store_event
from groundtruth.memory.predicate_families import map_predicate_family
from groundtruth.memory.retrieval.entity import entity_search
from groundtruth.utils.result import Ok


# ---- NER ----

def test_regex_ner_finds_languages() -> None:
    mentions = extract_entities_regex("We use Python and PostgreSQL with FastAPI")
    types = {m.entity_type for m in mentions}
    texts = {m.text for m in mentions}
    assert "Python" in texts
    assert "PostgreSQL" in texts
    assert "FastAPI" in texts
    assert "programming_language" in types
    assert "database" in types


def test_extract_entities_returns_results() -> None:
    """extract_entities (with fallback) should work regardless of GLiNER."""
    mentions = extract_entities("Docker and Kubernetes for deployment")
    assert len(mentions) >= 1
    texts = {m.text for m in mentions}
    assert "Docker" in texts or "Kubernetes" in texts


# ---- Entity Resolution ----

def test_entity_resolution_creates_new(memory_store: MemoryStore) -> None:
    from groundtruth.memory.enrich.ner import EntityMention

    eid = resolve_entity(
        memory_store,
        EntityMention(text="PostgreSQL", entity_type="database", score=0.9),
        "test-scope",
    )
    assert eid is not None

    # Verify in database
    row = memory_store.conn.execute(
        "SELECT * FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    assert row is not None
    assert row["canonical_name"] == "postgresql"
    assert row["entity_type"] == "database"


def test_entity_resolution_deduplicates(memory_store: MemoryStore) -> None:
    from groundtruth.memory.enrich.ner import EntityMention

    eid1 = resolve_entity(
        memory_store,
        EntityMention(text="PostgreSQL", entity_type="database", score=0.9),
        "test-scope",
    )
    eid2 = resolve_entity(
        memory_store,
        EntityMention(text="postgresql", entity_type="database", score=0.8),
        "test-scope",
    )
    assert eid1 == eid2

    # Mention count should be 2
    row = memory_store.conn.execute(
        "SELECT mention_count FROM entities WHERE id = ?", (eid1,)
    ).fetchone()
    assert row["mention_count"] == 2


def test_entity_resolution_scope_isolated(memory_store: MemoryStore) -> None:
    from groundtruth.memory.enrich.ner import EntityMention

    eid_a = resolve_entity(
        memory_store,
        EntityMention(text="React", entity_type="framework", score=0.9),
        "scope-a",
    )
    eid_b = resolve_entity(
        memory_store,
        EntityMention(text="React", entity_type="framework", score=0.9),
        "scope-b",
    )
    # Different scopes → different entities
    assert eid_a != eid_b


# ---- Predicate Families ----

def test_predicate_family_mapping() -> None:
    assert map_predicate_family("switched to") == "technology_choice"
    assert map_predicate_family("prefers") == "preference"
    assert map_predicate_family("joined") == "employment"
    assert map_predicate_family("lives in") == "location"
    assert map_predicate_family("xyzzy gibberish") == "other"


# ---- Classification + Claims ----

def test_classify_by_rules_decision() -> None:
    result = classify_by_rules("We decided to use PostgreSQL for the database")
    assert result.item_type == "decision"
    assert result.confidence > 0


def test_classify_by_rules_extracts_claims() -> None:
    result = classify_by_rules("We switched from MySQL to PostgreSQL")
    assert len(result.claims) >= 1
    claim = result.claims[0]
    assert claim.predicate_family == "technology_choice"
    assert "postgresql" in claim.object_text.lower()
    assert "explicit_switch" in result.supersession_signals


def test_classify_by_rules_preference() -> None:
    result = classify_by_rules("I prefer dark mode in all my editors")
    assert result.item_type == "preference"


# ---- Materialization ----

@pytest.mark.asyncio
async def test_materialize_creates_items_and_claims(memory_store: MemoryStore) -> None:
    r = await store_event(memory_store, "We switched from MySQL to PostgreSQL", "mat-test")
    assert isinstance(r, Ok)
    event_id = r.value["event_id"]

    classification = classify_by_rules("We switched from MySQL to PostgreSQL")
    item_result = materialize_item(memory_store, event_id, "mat-test", classification)
    assert isinstance(item_result, Ok)
    item_id = item_result.value

    claims_result = materialize_claims(memory_store, item_id, "mat-test", classification, {})
    assert isinstance(claims_result, Ok)
    assert len(claims_result.value) >= 1

    # Verify in database
    row = memory_store.conn.execute(
        "SELECT * FROM memory_items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row is not None
    assert row["item_type"] == "decision"

    claim_rows = memory_store.conn.execute(
        "SELECT * FROM claims WHERE item_id = ?", (item_id,)
    ).fetchall()
    assert len(claim_rows) >= 1


# ---- Full enrichment pipeline ----

@pytest.mark.asyncio
async def test_full_enrichment_pipeline(memory_store: MemoryStore) -> None:
    """Full pipeline: store → enrich → verify items, claims, entities exist."""
    config = MemoryConfig()
    r = await store_event(memory_store, "We use Python with FastAPI for the backend API", "full-test")
    assert isinstance(r, Ok)
    event_id = r.value["event_id"]

    await _enrich_event(memory_store, event_id, config)

    # Verify memory_item created
    items = memory_store.conn.execute(
        "SELECT * FROM memory_items WHERE event_id = ?", (event_id,)
    ).fetchall()
    assert len(items) >= 1

    # Verify entities created (Python, FastAPI at minimum via regex NER)
    entities = memory_store.conn.execute(
        "SELECT * FROM entities WHERE scope_id = 'full-test'"
    ).fetchall()
    assert len(entities) >= 1

    # Verify memory_entities links exist
    links = memory_store.conn.execute(
        "SELECT * FROM memory_entities"
    ).fetchall()
    assert len(links) >= 1


# ---- Entity-based retrieval ----

@pytest.mark.asyncio
async def test_entity_retrieval_finds_linked_events(memory_store: MemoryStore) -> None:
    """Entity search should find events linked to queried entities."""
    config = MemoryConfig()

    events = [
        "We use PostgreSQL for data storage",
        "The team meeting is every Monday at 10am",
        "PostgreSQL migration to version 16 complete",
    ]
    for text in events:
        r = await store_event(memory_store, text, "ent-test")
        assert isinstance(r, Ok)
        await _enrich_event(memory_store, r.value["event_id"], config)

    # Search for PostgreSQL-related events
    result = await entity_search(memory_store, "PostgreSQL", "ent-test")
    assert isinstance(result, Ok)
    # Should find the two PostgreSQL events, not the meeting event
    assert len(result.value) >= 1
    for r in result.value:
        assert "PostgreSQL" in r.content or "postgresql" in r.content.lower()
