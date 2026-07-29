"""Tests for Phase A: Claim verification via NLI grounding.

Tests that the verification module correctly identifies grounded vs
ungrounded claims against source text.
"""

from __future__ import annotations

import pytest

from groundtruth.memory.enrich.verify import (
    GroundingDecision,
    VerificationResult,
    format_claim_for_verification,
)


# ---------------------------------------------------------------------------
# Unit tests for claim formatting
# ---------------------------------------------------------------------------


def test_format_claim_basic():
    result = format_claim_for_verification("project", "uses", "PostgreSQL")
    assert result == "project uses PostgreSQL"


def test_format_claim_with_context():
    result = format_claim_for_verification("project", "uses", "PostgreSQL", "for database")
    assert result == "project uses PostgreSQL for database"


def test_format_claim_empty_context():
    result = format_claim_for_verification("backend", "uses", "Python", "")
    assert result == "backend uses Python"


def test_format_claim_strips_whitespace():
    result = format_claim_for_verification("  project ", " uses ", " Go  ")
    assert result == "project uses Go"


# ---------------------------------------------------------------------------
# NLI model tests (require sentence-transformers + model download)
# Skip if dependencies not available
# ---------------------------------------------------------------------------


@pytest.fixture
def nli_available():
    """Skip tests if sentence-transformers is not installed."""
    try:
        from sentence_transformers import CrossEncoder  # noqa: F401
        return True
    except ImportError:
        pytest.skip("sentence-transformers not installed")


def test_grounded_claim(nli_available):
    """A claim that IS supported by the source text should be GROUNDED."""
    from groundtruth.memory.enrich.verify import verify_claim

    result = verify_claim(
        claim_text="project uses PostgreSQL",
        source_text="We decided to go with Postgres for the user service database.",
    )
    assert isinstance(result, VerificationResult)
    assert result.grounding_score > 0.4  # Should have reasonable entailment
    assert result.decision in (GroundingDecision.GROUNDED, GroundingDecision.WEAK)


def test_ungrounded_claim(nli_available):
    """A claim NOT supported by source text should be UNGROUNDED or WEAK."""
    from groundtruth.memory.enrich.verify import verify_claim

    result = verify_claim(
        claim_text="project uses MongoDB",
        source_text="We decided to go with Postgres for the user service database.",
    )
    assert isinstance(result, VerificationResult)
    # MongoDB contradicts Postgres — should score low on entailment
    assert result.decision != GroundingDecision.GROUNDED


def test_batch_verification(nli_available):
    """Batch verification should return one result per pair."""
    from groundtruth.memory.enrich.verify import verify_claim_batch

    pairs = [
        ("project uses PostgreSQL", "We use Postgres for our database."),
        ("team size is 12", "The team grew from 5 to 12 engineers."),
        ("project uses MongoDB", "We decided to go with Postgres."),
    ]
    results = verify_claim_batch(pairs)
    assert len(results) == 3
    assert all(isinstance(r, VerificationResult) for r in results)


def test_empty_batch(nli_available):
    """Empty batch should return empty list."""
    from groundtruth.memory.enrich.verify import verify_claim_batch

    results = verify_claim_batch([])
    assert results == []


def test_verification_result_fields(nli_available):
    """VerificationResult should have all expected fields."""
    from groundtruth.memory.enrich.verify import verify_claim

    result = verify_claim(
        claim_text="backend uses Python",
        source_text="Our backend is written in Python with FastAPI.",
    )
    assert 0.0 <= result.grounding_score <= 1.0
    assert isinstance(result.decision, GroundingDecision)
    assert result.label in ("entailment", "contradiction", "neutral")
