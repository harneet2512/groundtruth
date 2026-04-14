"""Unit tests for the evidence-diversity promotion layer.

These tests verify that promotion logic is driven by evidence class diversity
and strength, not by raw support count.
"""

from __future__ import annotations

from groundtruth.substrate.promotion import promote_tier, weighted_score


class TestPromoteTier:
    # ------------------------------------------------------------------
    # Verified cases
    # ------------------------------------------------------------------

    def test_test_plus_callers_is_verified(self) -> None:
        """Strong class + strong class >= 4 weight."""
        assert promote_tier(["tests", "callers"]) == "verified"

    def test_test_plus_siblings_is_verified(self) -> None:
        """tests(3) + siblings_or_pairs(2) = 5 >= 4, has strong class."""
        assert promote_tier(["tests", "siblings_or_pairs"]) == "verified"

    def test_runtime_plus_callers_is_verified(self) -> None:
        """runtime_or_exec(3) + callers(2) = 5 >= 4, has strong class."""
        assert promote_tier(["runtime_or_exec", "callers"]) == "verified"

    def test_callers_plus_siblings_is_verified(self) -> None:
        """callers(2) + siblings_or_pairs(2) = 4 >= 4, has strong class."""
        assert promote_tier(["callers", "siblings_or_pairs"]) == "verified"

    def test_all_classes_is_verified(self) -> None:
        assert promote_tier(
            ["tests", "runtime_or_exec", "callers", "siblings_or_pairs", "structure", "docs_or_config"]
        ) == "verified"

    # ------------------------------------------------------------------
    # The critical anti-pattern: many callers alone must NOT be verified
    # ------------------------------------------------------------------

    def test_three_caller_examples_alone_is_likely(self) -> None:
        """Three caller-derived contracts = callers class only = score 2, still likely."""
        # Even though there are three sources, they all come from the same class.
        # Duplicate class labels must be collapsed.
        assert promote_tier(["callers", "callers", "callers"]) == "likely"

    def test_single_caller_is_likely(self) -> None:
        assert promote_tier(["callers"]) == "likely"

    def test_single_test_is_likely(self) -> None:
        """tests alone = score 3 < 4 with no second class, but score >= 2."""
        assert promote_tier(["tests"]) == "likely"

    def test_single_runtime_is_likely(self) -> None:
        assert promote_tier(["runtime_or_exec"]) == "likely"

    # ------------------------------------------------------------------
    # Likely cases
    # ------------------------------------------------------------------

    def test_sibling_plus_structure_is_likely(self) -> None:
        """siblings_or_pairs(2) + structure(1) = 3 >= 2, no strong class."""
        assert promote_tier(["siblings_or_pairs", "structure"]) == "likely"

    def test_docs_plus_structure_is_likely(self) -> None:
        """docs_or_config(1) + structure(1) = 2 >= 2."""
        assert promote_tier(["docs_or_config", "structure"]) == "likely"

    # ------------------------------------------------------------------
    # Possible cases
    # ------------------------------------------------------------------

    def test_single_structure_is_possible(self) -> None:
        """structure(1) = 1 < 2."""
        assert promote_tier(["structure"]) == "possible"

    def test_single_docs_is_possible(self) -> None:
        """docs_or_config(1) = 1 < 2."""
        assert promote_tier(["docs_or_config"]) == "possible"

    def test_empty_is_possible(self) -> None:
        assert promote_tier([]) == "possible"

    # ------------------------------------------------------------------
    # Score >= 4 without a strong class stays at likely
    # ------------------------------------------------------------------

    def test_high_score_without_strong_class_is_likely(self) -> None:
        """siblings_or_pairs(2) + docs_or_config(1) + structure(1) = 4,
        but no strong class (tests/runtime/callers). Must stay likely."""
        assert promote_tier(["siblings_or_pairs", "docs_or_config", "structure"]) == "likely"

    # ------------------------------------------------------------------
    # Duplicate deduplication
    # ------------------------------------------------------------------

    def test_duplicate_support_kinds_are_collapsed(self) -> None:
        """Passing the same kind multiple times must not inflate the score."""
        # siblings_or_pairs alone = score 2 = likely
        assert promote_tier(["siblings_or_pairs"] * 10) == "likely"

    def test_order_does_not_matter(self) -> None:
        result_a = promote_tier(["callers", "tests"])
        result_b = promote_tier(["tests", "callers"])
        assert result_a == result_b == "verified"


class TestWeightedScore:
    def test_empty(self) -> None:
        assert weighted_score([]) == 0

    def test_single_strong_class(self) -> None:
        assert weighted_score(["tests"]) == 3

    def test_deduplication(self) -> None:
        assert weighted_score(["tests", "tests"]) == 3

    def test_all_classes(self) -> None:
        score = weighted_score(
            ["tests", "runtime_or_exec", "callers", "siblings_or_pairs", "structure", "docs_or_config"]
        )
        assert score == 3 + 3 + 2 + 2 + 1 + 1

    def test_unknown_kind_is_zero(self) -> None:
        # mypy would flag this, but runtime must not crash
        assert weighted_score(["unknown_family"]) == 0  # type: ignore[arg-type]
