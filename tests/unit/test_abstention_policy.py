"""Tests for the abstention policy decision table."""

from __future__ import annotations

from groundtruth.policy.abstention import (
    AbstentionPolicy,
    EmissionLevel,
    MIN_COVERAGE_THRESHOLD,
    MIN_EVIDENCE_COUNT,
    TrustTier,
)

policy = AbstentionPolicy()


# --- RED trust: always EMIT_NOTHING ---


def test_red_always_emits_nothing_high_evidence() -> None:
    assert policy.decide(TrustTier.RED, evidence_count=100, coverage=50.0) == EmissionLevel.EMIT_NOTHING


def test_red_always_emits_nothing_fresh() -> None:
    assert policy.decide(TrustTier.RED, evidence_count=10, coverage=20.0, is_stale=False) == EmissionLevel.EMIT_NOTHING


def test_red_always_emits_nothing_stale() -> None:
    assert policy.decide(TrustTier.RED, evidence_count=10, coverage=20.0, is_stale=True) == EmissionLevel.EMIT_NOTHING


def test_red_should_emit_false() -> None:
    assert policy.should_emit(TrustTier.RED, evidence_count=50, coverage=100.0) is False


def test_red_zero_evidence() -> None:
    assert policy.decide(TrustTier.RED, evidence_count=0, coverage=0.0) == EmissionLevel.EMIT_NOTHING


# --- YELLOW + stale: always EMIT_NOTHING ---


def test_yellow_stale_emits_nothing() -> None:
    assert policy.decide(TrustTier.YELLOW, evidence_count=10, coverage=20.0, is_stale=True) == EmissionLevel.EMIT_NOTHING


def test_yellow_stale_high_evidence_still_nothing() -> None:
    assert policy.decide(TrustTier.YELLOW, evidence_count=100, coverage=100.0, is_stale=True) == EmissionLevel.EMIT_NOTHING


def test_yellow_stale_should_emit_false() -> None:
    assert policy.should_emit(TrustTier.YELLOW, evidence_count=50, coverage=50.0, is_stale=True) is False


# --- YELLOW + fresh + insufficient evidence ---


def test_yellow_fresh_below_min_evidence_emits_nothing() -> None:
    assert policy.decide(TrustTier.YELLOW, evidence_count=1, coverage=20.0) == EmissionLevel.EMIT_NOTHING


def test_yellow_fresh_zero_evidence_emits_nothing() -> None:
    assert policy.decide(TrustTier.YELLOW, evidence_count=0, coverage=20.0) == EmissionLevel.EMIT_NOTHING


def test_yellow_fresh_below_min_coverage_emits_nothing() -> None:
    assert policy.decide(TrustTier.YELLOW, evidence_count=5, coverage=4.0) == EmissionLevel.EMIT_NOTHING


# --- YELLOW + fresh + sufficient evidence ---


def test_yellow_fresh_sufficient_emits_soft_info() -> None:
    assert policy.decide(TrustTier.YELLOW, evidence_count=2, coverage=5.0) == EmissionLevel.EMIT_SOFT_INFO


def test_yellow_fresh_at_exact_threshold() -> None:
    result = policy.decide(
        TrustTier.YELLOW,
        evidence_count=MIN_EVIDENCE_COUNT,
        coverage=MIN_COVERAGE_THRESHOLD,
    )
    assert result == EmissionLevel.EMIT_SOFT_INFO


def test_yellow_fresh_should_emit_true() -> None:
    assert policy.should_emit(TrustTier.YELLOW, evidence_count=5, coverage=10.0) is True


# --- GREEN trust ---


def test_green_contradiction_emits_hard_blocker() -> None:
    result = policy.decide(TrustTier.GREEN, evidence_count=1, coverage=1.0, is_contradiction=True)
    assert result == EmissionLevel.EMIT_HARD_BLOCKER


def test_green_obligation_emits_soft_info() -> None:
    result = policy.decide(TrustTier.GREEN, evidence_count=1, coverage=1.0, is_contradiction=False)
    assert result == EmissionLevel.EMIT_SOFT_INFO


def test_green_stale_contradiction_still_hard_blocker() -> None:
    result = policy.decide(TrustTier.GREEN, evidence_count=1, coverage=1.0, is_stale=True, is_contradiction=True)
    assert result == EmissionLevel.EMIT_HARD_BLOCKER


def test_green_should_emit_true() -> None:
    assert policy.should_emit(TrustTier.GREEN, evidence_count=1, coverage=0.0) is True


def test_green_zero_evidence_still_emits() -> None:
    """GREEN trust means runtime-confirmed — even zero extra evidence is fine."""
    result = policy.decide(TrustTier.GREEN, evidence_count=0, coverage=0.0, is_contradiction=True)
    assert result == EmissionLevel.EMIT_HARD_BLOCKER


# --- Custom thresholds ---


def test_custom_min_evidence_threshold() -> None:
    strict = AbstentionPolicy(min_evidence=5)
    assert strict.decide(TrustTier.YELLOW, evidence_count=4, coverage=10.0) == EmissionLevel.EMIT_NOTHING
    assert strict.decide(TrustTier.YELLOW, evidence_count=5, coverage=10.0) == EmissionLevel.EMIT_SOFT_INFO


def test_custom_min_coverage_threshold() -> None:
    strict = AbstentionPolicy(min_coverage=10.0)
    assert strict.decide(TrustTier.YELLOW, evidence_count=5, coverage=9.0) == EmissionLevel.EMIT_NOTHING
    assert strict.decide(TrustTier.YELLOW, evidence_count=5, coverage=10.0) == EmissionLevel.EMIT_SOFT_INFO
