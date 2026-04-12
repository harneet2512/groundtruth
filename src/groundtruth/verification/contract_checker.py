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
        elif contract.contract_type == "type_shape":
            return self._check_type_shape(candidate, contract)
        elif contract.contract_type == "obligation":
            return self._check_obligation(candidate, contract)
        elif contract.contract_type == "negative_contract":
            return self._check_negative(candidate, contract)
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
        """Check output contract conservatively from obvious diff changes."""
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

        added_return_literals = _get_added_return_literals(candidate.diff)
        if expected_type == "tuple" and any(lit == "None" for lit in added_return_literals):
            return _violation(
                contract,
                "Added `return None` where tuple output is expected",
            )
        if expected_type == "dict" and any(lit.startswith("[") for lit in added_return_literals):
            return _violation(
                contract,
                "Added list return where dict output is expected",
            )
        if expected_type == "list" and any(lit.startswith("{") for lit in added_return_literals):
            return _violation(
                contract,
                "Added dict return where list output is expected",
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

    def _check_type_shape(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check type/shape contracts using signature and return changes."""
        normalized = contract.normalized_form
        if normalized.startswith("type_shape:destructurable:"):
            if any(lit == "None" for lit in _get_added_return_literals(candidate.diff)):
                return _violation(
                    contract,
                    "Added `return None` where callers destructure the return value",
                )
            return None

        parts = normalized.split(":", 2)
        if len(parts) >= 2 and parts[1] and parts[1] not in {"destructurable", "sibling_mismatch"}:
            expected_type = parts[1]
            return self._check_output(
                candidate,
                ContractRecord(
                    contract_type="exact_output",
                    scope_kind=contract.scope_kind,
                    scope_ref=contract.scope_ref,
                    predicate=contract.predicate,
                    normalized_form=f"returns:{expected_type}",
                    support_sources=contract.support_sources,
                    support_count=contract.support_count,
                    confidence=contract.confidence,
                    tier=contract.tier,
                ),
            )
        return None

    def _check_obligation(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check machine-verifiable obligation contracts."""
        normalized = contract.normalized_form
        if normalized.startswith("obligation:arity:"):
            if _signature_changed(candidate.diff):
                return _violation(
                    contract,
                    "Changed function signature while callers depend on current arity",
                )
            return None

        if normalized.startswith("obligation:exception:"):
            exc_type = normalized.split(":", 3)[2]
            return self._check_exception(
                candidate,
                ContractRecord(
                    contract_type="exception_message",
                    scope_kind=contract.scope_kind,
                    scope_ref=contract.scope_ref,
                    predicate=contract.predicate,
                    normalized_form=f"raises:{exc_type}:",
                    support_sources=contract.support_sources,
                    support_count=contract.support_count,
                    confidence=contract.confidence,
                    tier=contract.tier,
                ),
            )
        return None

    def _check_negative(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check negative contracts conservatively."""
        normalized = contract.normalized_form
        if normalized.startswith("negative:must_raise:") or normalized.startswith("negative:guard_raise:"):
            parts = normalized.split(":")
            if len(parts) >= 3:
                exc_type = parts[2]
                return self._check_exception(
                    candidate,
                    ContractRecord(
                        contract_type="exception_message",
                        scope_kind=contract.scope_kind,
                        scope_ref=contract.scope_ref,
                        predicate=contract.predicate,
                        normalized_form=f"raises:{exc_type}:",
                        support_sources=contract.support_sources,
                        support_count=contract.support_count,
                        confidence=contract.confidence,
                        tier=contract.tier,
                    ),
                )
            return None

        if normalized.startswith("negative:must_not_be_none:"):
            if any(lit == "None" for lit in _get_added_return_literals(candidate.diff)):
                return _violation(
                    contract,
                    "Added `return None` despite non-None negative contract",
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


def _get_added_return_literals(diff: str) -> list[str]:
    """Extract added return expressions from the diff."""
    literals = []
    for line in _get_added_lines(diff):
        stripped = line.strip()
        if stripped.startswith("return "):
            literals.append(stripped[len("return "):].strip())
    return literals


def _signature_changed(diff: str) -> bool:
    """Return True when a function definition line changed in the patch."""
    removed_defs = {
        line.strip()
        for line in _get_removed_lines(diff)
        if line.lstrip().startswith("def ")
    }
    added_defs = {
        line.strip()
        for line in _get_added_lines(diff)
        if line.lstrip().startswith("def ")
    }
    return bool(removed_defs and added_defs and removed_defs != added_defs)


def _violation(contract: ContractRecord, explanation: str) -> ViolationRecord:
    """Construct a severity-aware violation record."""
    severity = "hard" if contract.tier == "verified" else "soft"
    return ViolationRecord(
        contract_id=0,
        contract_type=contract.contract_type,
        predicate=contract.predicate,
        severity=severity,
        explanation=explanation,
    )
