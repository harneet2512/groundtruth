"""Tests for Phase C: Admission gate.

Tests that noise and duplicate events are filtered before enrichment.
"""

from __future__ import annotations

from groundtruth.memory.enrich.admission import (
    AdmissionDecision,
    should_admit,
    _is_noise,
)


# ---------------------------------------------------------------------------
# Noise detection tests
# ---------------------------------------------------------------------------


def test_noise_greeting():
    assert _is_noise("hi") is True
    assert _is_noise("hello!") is True
    assert _is_noise("Hey") is True


def test_noise_acknowledgment():
    assert _is_noise("ok") is True
    assert _is_noise("sure") is True
    assert _is_noise("sounds good") is True
    assert _is_noise("got it") is True
    assert _is_noise("thanks") is True


def test_noise_short():
    assert _is_noise("k") is True
    assert _is_noise("") is True
    assert _is_noise("  ") is True


def test_noise_filler():
    assert _is_noise("lol") is True
    assert _is_noise("haha") is True
    assert _is_noise("nice") is True


def test_not_noise_real_content():
    assert _is_noise("We use PostgreSQL for the database") is False
    assert _is_noise("I switched from MySQL to Postgres") is False
    assert _is_noise("The backend is written in Python") is False


def test_not_noise_preference():
    assert _is_noise("I prefer dark mode in all my editors") is False


# ---------------------------------------------------------------------------
# Full admission decision tests (without embedding novelty)
# ---------------------------------------------------------------------------


def test_admit_real_content(memory_store, memory_config):
    decision = should_admit("We use PostgreSQL for the database", "test-scope", memory_store, memory_config)
    assert isinstance(decision, AdmissionDecision)
    assert decision.admitted is True
    assert decision.reason == "admitted"


def test_reject_noise(memory_store, memory_config):
    decision = should_admit("sounds good", "test-scope", memory_store, memory_config)
    assert decision.admitted is False
    assert decision.reason == "noise_pattern"


def test_reject_too_short(memory_store, memory_config):
    decision = should_admit("yes ok", "test-scope", memory_store, memory_config)
    assert decision.admitted is False


def test_reject_empty(memory_store, memory_config):
    decision = should_admit("", "test-scope", memory_store, memory_config)
    assert decision.admitted is False


def test_admit_decision_structure(memory_store, memory_config):
    decision = should_admit("The team migrated to PostgreSQL last quarter", "test-scope", memory_store, memory_config)
    assert isinstance(decision.admitted, bool)
    assert isinstance(decision.score, float)
    assert isinstance(decision.reason, str)
    assert 0.0 <= decision.score <= 1.0
