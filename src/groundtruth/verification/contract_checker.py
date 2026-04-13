"""Contract Checker — verifies a patch against mined contracts.

Given a diff and a set of contracts scoped to the changed symbols,
determines which contracts are preserved and which are violated.
"""

from __future__ import annotations

import re

from groundtruth.substrate.types import ContractRecord
from groundtruth.verification.models import PatchCandidate, ViolationRecord

_DEF_PATTERNS = (
    r"def\s+{name}\b",
    r"class\s+{name}\b",
    r"func\s+(?:\([^)]*\)\s*)?{name}\b",
    r"function\s+{name}\b",
    r"interface\s+{name}\b",
    r"struct\s+{name}\b",
    r"type\s+{name}\b",
)

_GENERIC_DEF_PREFIX = re.compile(
    r"^\s*(?:def|class|func|function|interface|struct|type)\s+"
)


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

        Verification gating (audit):
        - 'possible' contracts: SKIP entirely (should never reach here)
        - 'likely' contracts: violations are downgraded to severity='soft'
        - 'verified' contracts: violations are severity='hard' (may reject)

        Returns a ViolationRecord if the contract is broken, None if preserved.
        """
        # Possible-tier: do not participate in verification at all
        if contract.tier == "possible":
            return None

        violation = self._check_by_type(candidate, contract)

        # Likely-tier: can only produce soft warnings, never hard rejections
        if violation and contract.tier == "likely":
            return ViolationRecord(
                contract_id=violation.contract_id,
                contract_type=violation.contract_type,
                predicate=violation.predicate,
                severity="soft",  # Downgraded: likely contracts cannot hard-reject
                explanation=violation.explanation + " (confidence: likely — warn only)",
            )

        return violation

    def _check_by_type(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Route to type-specific checker."""
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
        elif contract.contract_type == "registry_coupling":
            return self._check_registry_coupling(candidate, contract)
        elif contract.contract_type in {"config_coupling", "doc_coupling"}:
            return self._check_file_coupling(candidate, contract)
        elif contract.contract_type == "protocol_invariant":
            return self._check_protocol_invariant(candidate, contract)
        elif contract.contract_type == "protocol_usage":
            return self._check_protocol_usage(candidate, contract)
        elif contract.contract_type == "constructor_invariant":
            return self._check_constructor_invariant(candidate, contract)
        elif contract.contract_type == "exact_render_string":
            return self._check_exact_render_string(candidate, contract)
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
        """Check roundtrip contract: are both encode/decode still present?

        Import-aware (audit issue #8): if a def is removed but an import
        of the same symbol is added, it's a move-to-module refactor, not a removal.
        """
        parts = contract.normalized_form.split(":")
        if len(parts) < 3:
            return None

        encode_sym = parts[1]
        decode_sym = parts[2]

        removed_lines = _get_removed_lines(candidate.diff)
        added_lines = _get_added_lines(candidate.diff)

        for sym in (encode_sym, decode_sym):
            removed_def = _definition_for_symbol_exists(removed_lines, sym)
            added_def = _definition_for_symbol_exists(added_lines, sym)

            if removed_def and not added_def:
                # Check: was it moved to an import? (not truly removed)
                import_pattern = re.compile(
                    rf"\bimport\s+.*{re.escape(sym)}|from\s+\S+\s+import\s+.*{re.escape(sym)}"
                )
                imported = any(import_pattern.search(line) for line in added_lines)
                if imported:
                    continue  # Moved to module — not a violation

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

    def _check_registry_coupling(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check registry/config-style coupling conservatively.

        Machine-defensible rule:
        - if a registered symbol definition is removed or renamed
        - and the referenced registry file was not edited in the same patch
        then the patch likely broke registration/coupling.
        """
        parts = contract.normalized_form.split(":", 4)
        if len(parts) < 5:
            return None

        symbol_name = parts[2]
        registry_file = parts[4]
        registry_touched = registry_file in candidate.changed_files

        if registry_touched:
            return None

        removed_symbol = _definition_for_symbol_exists(_get_removed_lines(candidate.diff), symbol_name)
        added_same_symbol = _definition_for_symbol_exists(_get_added_lines(candidate.diff), symbol_name)

        if removed_symbol and not added_same_symbol:
            return _violation(
                contract,
                f"Removed or renamed registered symbol '{symbol_name}' without updating {registry_file}",
            )

        return None

    def _check_file_coupling(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check config/doc coupling at file granularity.

        Conservative rule:
        - if a source file is changed
        - and a coupled config/doc file was not edited
        - then only warn on significant rename/removal style edits
        """
        parts = contract.normalized_form.split(":", 3)
        if len(parts) < 4:
            return None

        target_file = parts[2]
        coupled_file = parts[3]
        if target_file not in candidate.changed_files or coupled_file in candidate.changed_files:
            return None

        significant_change = _definition_removed_or_renamed(candidate.diff)
        if significant_change:
            return _violation(
                contract,
                f"Changed {target_file} without reviewing coupled file {coupled_file}",
            )
        return None

    def _check_protocol_invariant(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check general protocol invariants conservatively."""
        parts = contract.normalized_form.split(":", 2)
        if len(parts) < 3:
            return None

        invariant = parts[1]
        added_literals = _get_added_return_literals(candidate.diff)
        if invariant in {"destructurable", "iterable", "attr_access"}:
            if any(lit in {"None", "null", "nil", "undefined"} for lit in added_literals):
                return _violation(
                    contract,
                    f"Added null-like return despite {invariant} protocol invariant",
                )

        if invariant == "destructurable":
            if any(lit in {"True", "False"} or lit.isdigit() for lit in added_literals):
                return _violation(
                    contract,
                    "Added scalar return where callers expect a destructurable value",
                )

        if invariant == "truthy":
            if any(lit in {"None", "null", "nil", "undefined", "False", "0", "0.0"} for lit in added_literals):
                return _violation(
                    contract,
                    "Added falsey/null-like return where callers depend on truthiness",
                )

        return None

    def _check_protocol_usage(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check concrete protocol-usage contracts."""
        parts = contract.normalized_form.split(":", 2)
        if len(parts) < 3:
            return None

        usage = parts[1]
        added_literals = _get_added_return_literals(candidate.diff)
        nullish = {"None", "null", "nil", "undefined"}
        falsey = nullish | {"False", "0", "0.0"}

        if usage in {"destructurable", "iterable", "attr_access"} and any(
            lit in nullish for lit in added_literals
        ):
            return _violation(
                contract,
                f"Added null-like return despite {usage} usage contract",
            )

        if usage == "destructurable" and any(
            lit in {"True", "False"} or lit.isdigit() for lit in added_literals
        ):
            return _violation(
                contract,
                "Added scalar return where callers require a destructurable value",
            )

        if usage == "truthy" and any(lit in falsey for lit in added_literals):
            return _violation(
                contract,
                "Added falsey/null-like return where callers require truthy semantics",
            )
        return None

    def _check_constructor_invariant(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check machine-defensible constructor invariants."""
        parts = contract.normalized_form.split(":", 3)
        if len(parts) < 3:
            return None

        invariant = parts[1]
        if invariant == "signature":
            if _signature_changed(candidate.diff):
                return _violation(contract, "Changed constructor signature/init protocol")
            return None

        if invariant == "exception" and len(parts) >= 4:
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
                    support_kinds=contract.support_kinds,
                    scope_file=contract.scope_file,
                    checkable=contract.checkable,
                    freshness_state=contract.freshness_state,
                ),
            )

        if invariant == "attr_init" and len(parts) >= 4:
            attr_name = parts[2]
            removed_assign = any(
                re.search(rf"\b(?:self|this)\.{re.escape(attr_name)}\s*=", line)
                for line in _get_removed_lines(candidate.diff)
            )
            added_assign = any(
                re.search(rf"\b(?:self|this)\.{re.escape(attr_name)}\s*=", line)
                for line in _get_added_lines(candidate.diff)
            )
            if removed_assign and not added_assign:
                return _violation(
                    contract,
                    f"Stopped initializing required constructor attribute '{attr_name}'",
                )
        return None

    def _check_exact_render_string(
        self, candidate: PatchCandidate, contract: ContractRecord
    ) -> ViolationRecord | None:
        """Check exact render/output strings conservatively."""
        parts = contract.normalized_form.split(":", 2)
        if len(parts) < 3:
            return None

        expected = parts[2]
        removed = any(expected in line for line in _get_removed_lines(candidate.diff))
        added = any(expected in line for line in _get_added_lines(candidate.diff))
        if removed and not added:
            return _violation(
                contract,
                f"Removed verified render/output string {expected!r}",
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
        if _looks_like_definition_line(line)
    }
    added_defs = {
        line.strip()
        for line in _get_added_lines(diff)
        if _looks_like_definition_line(line)
    }
    return bool(removed_defs and added_defs and removed_defs != added_defs)


def _definition_removed_or_renamed(diff: str) -> bool:
    """Return True when the patch removes or renames a definition."""
    removed_defs = [
        line.strip()
        for line in _get_removed_lines(diff)
        if _looks_like_definition_line(line)
    ]
    added_defs = [
        line.strip()
        for line in _get_added_lines(diff)
        if _looks_like_definition_line(line)
    ]
    if removed_defs and not added_defs:
        return True
    if removed_defs and added_defs and set(removed_defs) != set(added_defs):
        return True
    return False


def _looks_like_definition_line(line: str) -> bool:
    return bool(_GENERIC_DEF_PREFIX.match(line.lstrip()))


def _definition_for_symbol_exists(lines: list[str], symbol_name: str) -> bool:
    for pattern in _DEF_PATTERNS:
        compiled = re.compile(pattern.format(name=re.escape(symbol_name)))
        if any(compiled.search(line) for line in lines):
            return True
    return False


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
