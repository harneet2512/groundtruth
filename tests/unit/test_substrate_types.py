"""Tests for substrate types — frozen dataclasses, confidence tiers."""

import pytest

from groundtruth.substrate.types import (
    ConfidenceTier,
    ContractRecord,
    EvidenceItem,
    LocalizationResult,
    LocalizationTarget,
    PatchScore,
    tier_from_confidence,
)


class TestTierFromConfidence:
    def test_verified_requires_multi_support(self):
        assert tier_from_confidence(0.95, support_count=2) == "verified"
        assert tier_from_confidence(0.95, support_count=3) == "verified"

    def test_high_confidence_single_source_is_likely(self):
        assert tier_from_confidence(0.95, support_count=1) == "likely"
        assert tier_from_confidence(0.85, support_count=1) == "likely"

    def test_low_confidence_is_possible(self):
        assert tier_from_confidence(0.5, support_count=1) == "possible"
        assert tier_from_confidence(0.3, support_count=2) == "possible"

    def test_boundary_values(self):
        assert tier_from_confidence(0.85, support_count=2) == "verified"
        assert tier_from_confidence(0.84, support_count=2) == "likely"
        assert tier_from_confidence(0.6, support_count=1) == "likely"
        assert tier_from_confidence(0.59, support_count=1) == "possible"


class TestEvidenceItem:
    def test_frozen(self):
        item = EvidenceItem(
            family="CALLER", score=3, name="foo", file="bar.py",
            line=10, source_code="foo()", summary="calls foo",
            confidence=0.9, tier="likely",
        )
        with pytest.raises(AttributeError):
            item.score = 5  # type: ignore

    def test_hashable(self):
        item = EvidenceItem(
            family="CALLER", score=3, name="foo", file="bar.py",
            line=10, source_code="foo()", summary="calls foo",
            confidence=0.9, tier="likely",
        )
        assert hash(item) is not None
        assert item in {item}

    def test_equality(self):
        args = dict(
            family="TEST", score=2, name="test_foo", file="test.py",
            line=5, source_code="assert True", summary="test",
            confidence=0.8, tier="likely",
        )
        a = EvidenceItem(**args)
        b = EvidenceItem(**args)
        assert a == b


class TestContractRecord:
    def test_frozen_and_hashable(self):
        contract = ContractRecord(
            contract_type="exception_message",
            scope_kind="function",
            scope_ref="mymod.my_func",
            predicate="raises ValueError",
            normalized_form="raises:ValueError",
            support_sources=("test.py:10", "caller.py:20"),
            support_count=2,
            confidence=0.95,
            tier="verified",
        )
        assert hash(contract) is not None
        with pytest.raises(AttributeError):
            contract.confidence = 0.5  # type: ignore

    def test_tuple_sources(self):
        contract = ContractRecord(
            contract_type="exact_output",
            scope_kind="method",
            scope_ref="cls.method",
            predicate="returns int",
            normalized_form="returns:int",
            support_sources=("a.py:1",),
            support_count=1,
            confidence=0.8,
            tier="likely",
        )
        assert isinstance(contract.support_sources, tuple)


class TestLocalizationTarget:
    def test_fields(self):
        target = LocalizationTarget(
            node_id=42,
            name="get_user",
            file_path="src/users.py",
            start_line=100,
            confidence=0.9,
            tier="verified",
            file_confidence=0.8,
            symbol_confidence=0.7,
            reasons=("name_match", "file_mentioned"),
        )
        assert target.node_id == 42
        assert target.tier == "verified"
        assert len(target.reasons) == 2


class TestLocalizationResult:
    def test_structural_unlocked(self):
        target = LocalizationTarget(
            node_id=1, name="f", file_path="a.py", start_line=1,
            confidence=0.9, tier="verified",
            file_confidence=0.8, symbol_confidence=0.7,
            reasons=("name_match",),
        )
        result = LocalizationResult(
            candidates=(target,),
            structural_unlocked=True,
            issue_identifiers=("foo", "bar"),
        )
        assert result.structural_unlocked is True
        assert len(result.issue_identifiers) == 2


class TestPatchScore:
    def test_decisions(self):
        score = PatchScore(
            candidate_id="patch-001",
            contract_score=0.9,
            test_score=0.8,
            maintainability_score=0.7,
            overall_score=0.85,
            decision="accept",
            reasons=(),
        )
        assert score.decision == "accept"
        assert score.overall_score == 0.85
