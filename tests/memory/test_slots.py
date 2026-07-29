"""Tests for Phase B: Slot-based state identity.

Tests that slot extraction and normalization produce correct,
consistent slot paths for supersession matching.
"""

from __future__ import annotations

import pytest

from groundtruth.memory.enrich.slots import (
    build_slot,
    extract_slot_from_claim,
    normalize_slot_component,
)


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


def test_normalize_lowercase():
    assert normalize_slot_component("Database") == "database"


def test_normalize_aliases():
    assert normalize_slot_component("db") == "database"
    assert normalize_slot_component("lang") == "language"
    assert normalize_slot_component("pg") == "postgresql"
    assert normalize_slot_component("k8s") == "kubernetes"


def test_normalize_strip_articles():
    assert normalize_slot_component("the backend") == "backend"
    assert normalize_slot_component("our database") == "database"
    assert normalize_slot_component("my preference") == "preference"


def test_normalize_possessive():
    assert normalize_slot_component("user's") == "user"
    assert normalize_slot_component("team's") == "team"


def test_normalize_whitespace():
    assert normalize_slot_component("  fitness  routine  ") == "fitness_routine"


# ---------------------------------------------------------------------------
# Slot building tests
# ---------------------------------------------------------------------------


def test_build_slot_basic():
    assert build_slot("database", "engine") == "database/engine"
    assert build_slot("backend", "language") == "backend/language"
    assert build_slot("user", "yoga_frequency") == "user/yoga_frequency"


def test_build_slot_with_aliases():
    assert build_slot("db", "lang") == "database/language"


# ---------------------------------------------------------------------------
# Slot extraction from claim fields
# ---------------------------------------------------------------------------


def test_extract_slot_database():
    slot = extract_slot_from_claim("database", "uses", "for engine")
    assert slot == "database/engine"


def test_extract_slot_backend_language():
    slot = extract_slot_from_claim("backend", "uses", "for language")
    assert slot == "backend/language"


def test_extract_slot_generic_predicate_with_context():
    """When predicate is generic ('uses'), context should provide specificity."""
    slot = extract_slot_from_claim("project", "uses", "for database")
    assert slot == "project/database"


def test_extract_slot_generic_predicate_no_context():
    """When predicate is generic and no context, fall back to attribute mapping."""
    slot = extract_slot_from_claim("project", "uses", "")
    assert slot == "project/technology"


def test_extract_slot_location():
    slot = extract_slot_from_claim("alice", "lives in", "")
    assert slot == "alice/location"


def test_extract_slot_fitness():
    slot = extract_slot_from_claim("fitness routine", "frequency", "yoga")
    # Should capture yoga in the attribute
    assert "fitness_routine" in slot


# ---------------------------------------------------------------------------
# Slot identity pair tests (from engineering design)
# ---------------------------------------------------------------------------


def test_same_slot_exact():
    """Identical strings should produce identical slots."""
    s1 = extract_slot_from_claim("database", "uses", "for engine")
    s2 = extract_slot_from_claim("database", "uses", "for engine")
    assert s1 == s2


def test_different_slot_different_entity():
    """Different entities should produce different slots."""
    s1 = extract_slot_from_claim("backend", "uses", "for language")
    s2 = extract_slot_from_claim("frontend", "uses", "for language")
    assert s1 != s2, f"Expected different slots, got {s1} and {s2}"


def test_different_slot_different_attribute():
    """Same entity but different attributes should produce different slots."""
    s1 = extract_slot_from_claim("project", "uses", "for database")
    s2 = extract_slot_from_claim("project", "uses", "for cache")
    assert s1 != s2, f"Expected different slots, got {s1} and {s2}"


def test_alice_vs_bob_location():
    """Different people, same attribute → different slots."""
    s1 = extract_slot_from_claim("alice", "lives in", "")
    s2 = extract_slot_from_claim("bob", "lives in", "")
    assert s1 != s2
    assert "alice/location" == s1
    assert "bob/location" == s2
