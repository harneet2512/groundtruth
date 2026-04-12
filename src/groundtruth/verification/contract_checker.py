"""Contract Checker — verifies a patch against mined contracts.

Given a diff and a set of contracts scoped to the changed symbols,
determines which contracts are preserved and which are violated.
"""

from __future__ import annotations

import re

from groundtruth.substrate.types import ContractRecord
from groundtruth.verification.models import PatchCandidate, ViolationRecord


class ContractChecker:
    """Checks a candidate patch against applicable contracts."""

    def check(
        self,
        candidate: PatchCandidate,
        contracts: list[ContractRecord],
    ) -> tuple[float, list[ViolationRecord]]:
        """Check all contracts against the patch.

        Returns:
            (score, violations) where score is 0.0-1.0 and violations
            are the specific contracts that were broken.
        """
        if not contracts:
            return 1.0, []

        violations: list[ViolationRecord] = []
        passed = 0
        total = 0

        for contract in contracts:
            total += 1
            violation = self._check_single(candidate, contract)
            if violation:
                violations.append(violation)
            else:
                passed += 1

        score = passed / total if total > 0 else 1.0
        return score, violations

    def _check_single(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check a single contract against the diff.

        Returns a ViolationRecord if the contract is broken, None if preserved.
        """
        if contract.contract_type == "exception_message":
            return self._check_exception(candidate, contract)
        elif contract.contract_type == "exact_output":
            return self._check_output(candidate, contract)
        elif contract.contract_type == "roundtrip":
            return self._check_roundtrip(candidate, contract)
        return None

    def _check_exception(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check exception contract: does the diff remove a raise statement?"""
        # Parse normalized_form: 'raises:ValueError:message'
        parts = contract.normalized_form.split(":", 2)
        if len(parts) < 2:
            return None

        exc_type = parts[1]

        # Check if the diff removes lines containing 'raise ExcType'
        removed_lines = _get_removed_lines(candidate.diff)
        added_lines = _get_added_lines(candidate.diff)

        # If raise ExcType was removed but not re-added → violation
        raise_pattern = re.compile(rf"\braise\s+{re.escape(exc_type)}\b")
        removed_raises = any(raise_pattern.search(line) for line in removed_lines)
        added_raises = any(raise_pattern.search(line) for line in added_lines)

        if removed_raises and not added_raises:
            severity = "hard" if contract.tier == "verified" else "soft"
            return ViolationRecord(
                contract_id=0,  # Will be filled by caller with actual DB id
                contract_type=contract.contract_type,
                predicate=contract.predicate,
                severity=severity,
                explanation=f"Removed raise {exc_type} without replacement",
            )

        return None

    def _check_output(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check output contract: does the diff change return type?"""
        # Parse normalized_form: 'returns:type'
        parts = contract.normalized_form.split(":", 1)
        if len(parts) < 2:
            return None

        expected_type = parts[1]

        # Check if return type annotation was changed
        removed_lines = _get_removed_lines(candidate.diff)
        added_lines = _get_added_lines(candidate.diff)

        # Look for return type annotation changes
        return_pattern = re.compile(r"->\s*(.+?)(?:\s*:|$)")
        removed_types = set()
        added_types = set()

        for line in removed_lines:
            match = return_pattern.search(line)
            if match:
                removed_types.add(match.group(1).strip())

        for line in added_lines:
            match = return_pattern.search(line)
            if match:
                added_types.add(match.group(1).strip())

        # If original return type was removed and replaced with different type
        if removed_types and added_types and expected_type in removed_types:
            if expected_type not in added_types:
                severity = "hard" if contract.tier == "verified" else "soft"
                return ViolationRecord(
                    contract_id=0,
                    contract_type=contract.contract_type,
                    predicate=contract.predicate,
                    severity=severity,
                    explanation=f"Changed return type from {expected_type} to {added_types}",
                )

        return None

    def _check_roundtrip(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check roundtrip contract: are both encode/decode still present?"""
        # Parse normalized_form: 'roundtrip:encode_sym:decode_sym'
        parts = contract.normalized_form.split(":")
        if len(parts) < 3:
            return None

        encode_sym = parts[1]
        decode_sym = parts[2]

        # Check if either function was removed entirely
        removed_lines = _get_removed_lines(candidate.diff)
        added_lines = _get_added_lines(candidate.diff)

        # If a def for encode or decode was removed but not re-added
        for sym in (encode_sym, decode_sym):
            def_pattern = re.compile(rf"\bdef\s+{re.escape(sym)}\b")
            removed_def = any(def_pattern.search(line) for line in removed_lines)
            added_def = any(def_pattern.search(line) for line in added_lines)

            if removed_def and not added_def:
                severity = "hard" if contract.tier == "verified" else "soft"
                return ViolationRecord(
                    contract_id=0,
                    contract_type=contract.contract_type,
                    predicate=contract.predicate,
                    severity=severity,
                    explanation=f"Removed {sym}, breaking roundtrip with {encode_sym}/{decode_sym}",
                )

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_removed_lines(diff: str) -> list[str]:
    """Extract removed lines (starting with '-') from a unified diff."""
    lines = []
    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            lines.append(line[1:])
    return lines


def _get_added_lines(diff: str) -> list[str]:
    """Extract added lines (starting with '+') from a unified diff."""
    lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return lines
