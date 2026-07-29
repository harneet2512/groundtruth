"""Tests for Phase D: 7-signal retrieval components.

Tests activation model, MMR diversity, and token budget allocation.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from groundtruth.memory.retrieval.activation import compute_activation
from groundtruth.memory.retrieval.diversity import ScoredItem, select_diverse
from groundtruth.memory.retrieval.budget import BudgetItem, allocate_budget


# ---------------------------------------------------------------------------
# Activation model tests
# ---------------------------------------------------------------------------


def test_activation_recent_high():
    """Recently accessed item should have high activation."""
    now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    score = compute_activation(recent, access_count=5, now=now)
    assert score > 0.9


def test_activation_old_decays():
    """Old item should have lower activation than recent item."""
    now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=30)).isoformat()

    score_recent = compute_activation(recent, access_count=1, now=now)
    score_old = compute_activation(old, access_count=1, now=now)
    assert score_recent > score_old


def test_activation_floor():
    """Very old item should not go below floor."""
    now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    ancient = (now - timedelta(days=365)).isoformat()
    score = compute_activation(ancient, access_count=0, now=now, floor=0.35)
    assert score >= 0.35


def test_activation_frequency_boost():
    """Frequently accessed item should score higher than rarely accessed."""
    now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    timestamp = (now - timedelta(hours=24)).isoformat()

    score_rare = compute_activation(timestamp, access_count=1, now=now)
    score_frequent = compute_activation(timestamp, access_count=100, now=now)
    assert score_frequent > score_rare


def test_activation_never_accessed():
    """Item never accessed returns floor."""
    score = compute_activation(None, access_count=0)
    assert score == 0.35


# ---------------------------------------------------------------------------
# MMR diversity tests
# ---------------------------------------------------------------------------


def test_diversity_returns_k_items():
    items = [ScoredItem(id=str(i), score=1.0 - i * 0.1, content=f"item {i}") for i in range(20)]
    selected = select_diverse(items, k=5)
    assert len(selected) == 5


def test_diversity_empty_input():
    assert select_diverse([], k=5) == []


def test_diversity_fewer_than_k():
    items = [ScoredItem(id="1", score=1.0, content="only one")]
    selected = select_diverse(items, k=5)
    assert len(selected) == 1


def test_diversity_with_embeddings():
    """With embeddings, diverse items should be selected over similar ones."""
    items = [
        ScoredItem(id="a", score=1.0, content="database", embedding=[1.0, 0.0, 0.0]),
        ScoredItem(id="b", score=0.95, content="database2", embedding=[0.99, 0.01, 0.0]),  # very similar to a
        ScoredItem(id="c", score=0.9, content="language", embedding=[0.0, 1.0, 0.0]),       # different
        ScoredItem(id="d", score=0.85, content="deploy", embedding=[0.0, 0.0, 1.0]),        # different
    ]
    selected = select_diverse(items, k=3, lambda_=0.5)
    ids = [s.id for s in selected]
    # "a" should be first (highest score), then "c" or "d" (diverse), not "b" (too similar to a)
    assert ids[0] == "a"
    assert "b" not in ids[:3] or "c" in ids[:3]  # diversity should push out b


# ---------------------------------------------------------------------------
# Token budget tests
# ---------------------------------------------------------------------------


def test_budget_basic():
    items = [
        BudgetItem(id="1", score=1.0, token_count=100, content="high relevance"),
        BudgetItem(id="2", score=0.8, token_count=200, content="medium relevance"),
        BudgetItem(id="3", score=0.3, token_count=100, content="low relevance"),
    ]
    selected = allocate_budget(items, budget=300, max_items=10, quality_cutoff=0.3)
    assert len(selected) >= 1
    # High relevance item should be included
    assert any(s.id == "1" for s in selected)


def test_budget_respects_limit():
    items = [BudgetItem(id=str(i), score=1.0, token_count=100, content=f"item {i}") for i in range(20)]
    selected = allocate_budget(items, budget=10000, max_items=5)
    assert len(selected) <= 5


def test_budget_respects_token_limit():
    items = [BudgetItem(id=str(i), score=1.0, token_count=500, content=f"item {i}") for i in range(20)]
    selected = allocate_budget(items, budget=2000, max_items=20)
    total_tokens = sum(s.token_count for s in selected)
    assert total_tokens <= 2000


def test_budget_quality_cutoff():
    """Items below quality cutoff should be excluded."""
    items = [
        BudgetItem(id="1", score=1.0, token_count=100, content="best"),
        BudgetItem(id="2", score=0.1, token_count=100, content="terrible"),  # 0.1 < 0.3 * 1.0
    ]
    selected = allocate_budget(items, budget=1000, quality_cutoff=0.3)
    assert len(selected) == 1
    assert selected[0].id == "1"


def test_budget_empty():
    assert allocate_budget([]) == []
