"""Phase 1 tests: embedding pipeline, vector retrieval, hybrid retrieval."""

from __future__ import annotations

import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.memory.enrich.embed import embed_query, embed_passage, embed_batch
from groundtruth.memory.ingest.sync import store_event
from groundtruth.memory.retrieval.keyword import retrieve, fts_search
from groundtruth.memory.retrieval.vector import vector_search
from groundtruth.utils.result import Ok


# ---- Step 1.1: Embedding pipeline ----

def test_embed_query_returns_384_dims() -> None:
    vec = embed_query("What language does the team use?")
    assert len(vec) == 384
    assert all(isinstance(v, float) for v in vec)


def test_embed_passage_returns_384_dims() -> None:
    vec = embed_passage("The team uses Python for backend development.")
    assert len(vec) == 384


def test_embed_batch_returns_correct_count() -> None:
    vecs = embed_batch(["Hello", "World", "Test"])
    assert len(vecs) == 3
    assert all(len(v) == 384 for v in vecs)


def test_relevant_passage_scores_higher_than_irrelevant() -> None:
    """Query/passage prefix discipline: relevant > irrelevant."""
    import numpy as np

    qv = embed_query("What database does the project use?")
    relevant = embed_passage("The project uses PostgreSQL for data storage.")
    irrelevant = embed_passage("The weather forecast for tomorrow is sunny.")

    def cosine(a: list[float], b: list[float]) -> float:
        a_np, b_np = np.array(a), np.array(b)
        return float(np.dot(a_np, b_np) / (np.linalg.norm(a_np) * np.linalg.norm(b_np)))

    sim_rel = cosine(qv, relevant)
    sim_irr = cosine(qv, irrelevant)
    assert sim_rel > sim_irr, f"Relevant {sim_rel:.4f} should > irrelevant {sim_irr:.4f}"


# ---- Step 1.2: Enrichment worker generates embeddings ----

@pytest.mark.asyncio
async def test_enrichment_stores_embedding(memory_store: MemoryStore) -> None:
    """After store_event + manual enrichment, embedding should exist."""
    from groundtruth.memory.enrich.worker import _enrich_event
    from groundtruth.memory.config import MemoryConfig

    config = MemoryConfig()
    result = await store_event(memory_store, "Python is great for ML", "embed-test")
    assert isinstance(result, Ok)
    event_id = result.value["event_id"]

    await _enrich_event(memory_store, event_id, config)

    row = memory_store.conn.execute(
        "SELECT * FROM event_embeddings WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    assert row["model_name"] == "intfloat/e5-small-v2"
    assert row["embedding_dim"] == 384


def test_embed_model_abstraction_uses_configurable_model() -> None:
    from groundtruth.memory.enrich.embed import get_embedding_model

    model = get_embedding_model("intfloat/e5-small-v2", 384)
    assert model.model_name == "intfloat/e5-small-v2"
    assert model.dim == 384


# ---- Step 1.3: Vector retrieval ----

@pytest.mark.asyncio
async def test_vector_search_finds_matching(memory_store: MemoryStore) -> None:
    """Vector search should find semantically similar events."""
    from groundtruth.memory.enrich.worker import _enrich_event

    config = MemoryConfig()

    # Store and enrich events with clearly distinct topics
    events = [
        "The team adopted a new code review policy requiring two approvals",
        "Our weekly standup meetings are held every Monday at 10am",
        "PostgreSQL database migration from version 14 to 16 completed successfully",
    ]
    for text in events:
        r = await store_event(memory_store, text, "vec-test")
        assert isinstance(r, Ok)
        await _enrich_event(memory_store, r.value["event_id"], config)

    # Search for database-related content — should find the PostgreSQL event
    result = await vector_search(memory_store, "database migration upgrade", "vec-test")
    assert isinstance(result, Ok)
    assert len(result.value) >= 1
    # PostgreSQL event should rank highest — query is clearly about databases
    assert "PostgreSQL" in result.value[0].content


# ---- Step 1.5: Scope filtering on vector path ----

@pytest.mark.asyncio
async def test_vector_scope_isolation(memory_store: MemoryStore) -> None:
    """Vector search must not return results from other scopes."""
    from groundtruth.memory.enrich.worker import _enrich_event

    config = MemoryConfig()

    r_a = await store_event(memory_store, "Secret Alpha project uses Rust", "scope-a")
    assert isinstance(r_a, Ok)
    await _enrich_event(memory_store, r_a.value["event_id"], config)

    r_b = await store_event(memory_store, "Secret Beta project uses Java", "scope-b")
    assert isinstance(r_b, Ok)
    await _enrich_event(memory_store, r_b.value["event_id"], config)

    # Query scope-a, should NOT see scope-b
    result = await vector_search(memory_store, "project language", "scope-a")
    assert isinstance(result, Ok)
    for r in result.value:
        assert r.scope_id == "scope-a", f"Leaked: {r.scope_id}"


# ---- Step 1.4: Hybrid retrieval beats single-channel ----

@pytest.mark.asyncio
async def test_hybrid_retrieval_uses_both_channels(memory_store: MemoryStore) -> None:
    """Hybrid retrieve() should use both FTS and vector results."""
    from groundtruth.memory.enrich.worker import _enrich_event

    config = MemoryConfig()

    events = [
        "The team switched from MySQL to PostgreSQL in Q3",
        "Frontend performance improved after migrating to Next.js",
        "Code review policy requires two approvals minimum",
    ]
    for text in events:
        r = await store_event(memory_store, text, "hybrid-test")
        assert isinstance(r, Ok)
        await _enrich_event(memory_store, r.value["event_id"], config)

    # This query should match via both FTS (keyword "PostgreSQL") and vector (semantic "database")
    result = await retrieve(memory_store, "PostgreSQL database", "hybrid-test", config=config)
    assert isinstance(result, Ok)
    assert len(result.value) >= 1
    # The database event should be top result
    assert "PostgreSQL" in result.value[0].content


@pytest.mark.asyncio
async def test_hybrid_beats_fts_only_on_semantic(memory_store: MemoryStore) -> None:
    """Hybrid should find semantically relevant content that FTS misses."""
    from groundtruth.memory.enrich.worker import _enrich_event

    config = MemoryConfig()

    events = [
        "We deployed the application on AWS EC2 instances",
        "The infrastructure runs on cloud servers with auto-scaling",
        "Unit tests must cover at least 80% of the codebase",
    ]
    for text in events:
        r = await store_event(memory_store, text, "semantic-test")
        assert isinstance(r, Ok)
        await _enrich_event(memory_store, r.value["event_id"], config)

    # FTS-only: "hosting" doesn't appear in any event
    fts_result = await fts_search(memory_store, "hosting", "semantic-test")
    assert isinstance(fts_result, Ok)
    fts_count = len(fts_result.value)

    # Hybrid: "hosting" is semantically related to "deployed on AWS" and "cloud servers"
    hybrid_result = await retrieve(memory_store, "hosting", "semantic-test", config=config)
    assert isinstance(hybrid_result, Ok)
    hybrid_count = len(hybrid_result.value)

    # Hybrid should find more results than FTS for purely semantic queries
    assert hybrid_count > fts_count, (
        f"Hybrid ({hybrid_count}) should find more than FTS ({fts_count}) for semantic queries"
    )
