"""Patch Scorer — composite scoring of candidate patches.

Combines contract checking, test selection, and maintainability heuristics
into a single VerificationResult with decision and reason codes.
"""

from __future__ import annotations

from groundtruth.substrate.protocols import GraphReader
from groundtruth.substrate.types import ContractRecord
from groundtruth.verification.contract_checker import ContractChecker
from groundtruth.verification.maintainability import MaintainabilityChecker
from groundtruth.verification.models import (
    PatchCandidate,
    VerificationResult,
    ViolationRecord,
)
from groundtruth.verification.test_selector import TestSelector


class PatchScorer:
    """Scores a candidate patch against contracts, tests, and maintainability.

    Decision rules:
    - reject: any hard violation (contract broken, support_count ≥ 2)
    - abstain: only soft violations, low overall confidence
    - accept: no violations, positive signal
    """

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader
        self._contract_checker = ContractChecker()
        self._test_selector = TestSelector(reader)
        self._maintainability_checker = MaintainabilityChecker()

    def score(
        self,
        candidate: PatchCandidate,
        contracts: list[ContractRecord],
        test_results: dict[str, bool] | None = None,
    ) -> VerificationResult:
        """Score a candidate patch.

        Args:
            candidate: The patch to score.
            contracts: Applicable contracts for the changed symbols.
            test_results: Optional dict of test_file → pass/fail.

        Returns a VerificationResult with scores and decision.
        """
        # 1. Check contracts
        contract_score, violations = self._contract_checker.check(
            candidate, contracts
        )

        # 2. Select tests
        recommended_tests = self._test_selector.select(
            list(candidate.changed_symbols),
            list(candidate.changed_files),
        )

        # 3. Score tests (if results provided)
        test_score = self._score_tests(test_results, recommended_tests)

        # 4. Check maintainability
        maint_score, maint_violations = self._maintainability_checker.check(
            candidate
        )
        violations = list(violations) + maint_violations

        # 5. Composite score
        overall = self._composite_score(contract_score, test_score, maint_score)

        # 6. Decision
        decision = self._decide(violations, overall)

        # 7. Reason codes
        reason_codes = self._build_reason_codes(violations)

        return VerificationResult(
            candidate_id=candidate.candidate_id,
            contract_score=contract_score,
            test_score=test_score,
            maintainability_score=maint_score,
            overall_score=overall,
            decision=decision,
            violations=tuple(violations),
            recommended_tests=tuple(recommended_tests),
            reason_codes=tuple(reason_codes),
        )

    def _score_tests(
        self,
        test_results: dict[str, bool] | None,
        recommended: list[str],
    ) -> float:
        """Score based on test results if available."""
        if not test_results:
            return 0.5  # Unknown — neutral score

        relevant = {k: v for k, v in test_results.items() if k in recommended}
        if not relevant:
            return 0.5

        passed = sum(1 for v in relevant.values() if v)
        return passed / len(relevant)

    def _composite_score(
        self, contract: float, test: float, maint: float
    ) -> float:
        """Weighted composite score.

        Weights: contracts > tests > maintainability
        """
        return contract * 0.5 + test * 0.3 + maint * 0.2

    def _decide(
        self, violations: list[ViolationRecord], overall: float
    ) -> str:
        """Make accept/reject/abstain decision.

        Rules (from engineering plan):
        - reject: any hard violation (contract broken with verified support)
        - abstain: only soft violations OR insufficient evidence
        - accept: no violations, positive signal (overall >= 0.7)

        Soft maintainability concerns NEVER veto alone.
        """
        hard_violations = [v for v in violations if v.severity == "hard"]

        if hard_violations:
            return "reject"
        if overall < 0.4:
            return "abstain"
        if overall >= 0.7:
            return "accept"
        # Between 0.4-0.7 with only soft violations → abstain (not enough signal)
        return "abstain"

    def _build_reason_codes(self, violations: list[ViolationRecord]) -> list[str]:
        """Generate machine-readable reason codes."""
        codes: list[str] = []
        for v in violations:
            if v.contract_type == "exception_message":
                codes.append("exception_removed")
            elif v.contract_type == "exact_output":
                codes.append("return_type_changed")
            elif v.contract_type == "roundtrip":
                codes.append("roundtrip_broken")
            elif v.severity == "soft":
                codes.append("maintainability_concern")
        return codes
