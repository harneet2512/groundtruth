"""NegativeExtractor — mines negative contracts (things that must NOT happen).

Negative contracts capture constraints from:
1. Failing test assertions (assertRaises = function MUST raise, not silently pass)
2. Guard clauses that prevent certain states
3. Callers that assert the function does NOT return certain values

These are powerful because violating a negative contract means
the patch silently broke expected error behavior.

Confidence model:
- assertRaises/pytest.raises in test: 0.95 (strong negative contract)
- assertNotEqual/assertIsNotNone in test: 0.85
- Guard clause preventing state: 0.80
"""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence


class NegativeExtractor:
    """Extracts negative contracts — things that must NOT happen."""

    contract_type = "negative_contract"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        """Extract negative contracts for a given symbol node."""
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        results: list[ContractRecord] = []

        # Source 1: assertRaises / pytest.raises (function MUST raise on bad input)
        results.extend(self._from_raise_assertions(reader, name, scope_kind, qualified))

        # Source 2: assertNotEqual / assertIsNotNone / assertFalse patterns
        results.extend(self._from_negative_assertions(reader, name, scope_kind, qualified))

        # Source 3: Guard clauses that validate input (must not pass silently)
        results.extend(self._from_guard_clauses(reader, node_id, scope_kind, qualified))

        return results

    def _from_raise_assertions(
        self, reader, name: str, scope_kind: str, scope_ref: str  # noqa: ANN001
    ) -> list[ContractRecord]:
        """Extract from assertRaises — function must raise on invalid input."""
        results: list[ContractRecord] = []
        assertions = reader.get_assertions_for_target(name)

        for a in assertions:
            kind = a.get("kind", "")
            if kind not in ("assertRaises", "raises", "assert_raises"):
                continue

            expected = a.get("expected", "")
            expression = a.get("expression", "")
            exc_type = expected.split("(")[0].strip() if expected else ""

            if not exc_type:
                # Try to parse from expression
                for prefix in ("assertRaises(", "raises("):
                    if prefix in expression:
                        rest = expression.split(prefix, 1)[1]
                        exc_type = rest.split(",")[0].split(")")[0].strip()
                        break

            if exc_type and exc_type[0].isupper():
                results.append(ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=scope_ref,
                    predicate=f"MUST raise {exc_type} on invalid input (test enforces)",
                    normalized_form=f"negative:must_raise:{exc_type}:{scope_ref}",
                    support_sources=(
                        f"{a.get('file_path', a.get('test_name', ''))}:{a.get('line', 0)}",
                    ),
                    support_count=1,
                    confidence=0.95,
                    tier=tier_from_confidence(0.95, 1),
                ))

        return results

    def _from_negative_assertions(
        self, reader, name: str, scope_kind: str, scope_ref: str  # noqa: ANN001
    ) -> list[ContractRecord]:
        """Extract from assertNotEqual, assertIsNotNone, assertFalse."""
        results: list[ContractRecord] = []
        assertions = reader.get_assertions_for_target(name)

        negative_kinds = {
            "assertNotEqual": "must_not_equal",
            "assertIsNotNone": "must_not_be_none",
            "assertFalse": "must_be_falsy",
            "assertIsNot": "must_not_be",
            "assert_ne": "must_not_equal",
        }

        for a in assertions:
            kind = a.get("kind", "")
            if kind not in negative_kinds:
                continue

            neg_type = negative_kinds[kind]
            expected = a.get("expected", "")
            expression = a.get("expression", "")

            predicate = f"Must NOT return {expected}" if expected else f"Return must satisfy {neg_type}"
            results.append(ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                predicate=predicate,
                normalized_form=f"negative:{neg_type}:{scope_ref}",
                support_sources=(
                    f"{a.get('file_path', a.get('test_name', ''))}:{a.get('line', 0)}",
                ),
                support_count=1,
                confidence=0.85,
                tier=tier_from_confidence(0.85, 1),
            ))

        return results

    def _from_guard_clauses(
        self, reader, node_id: int, scope_kind: str, scope_ref: str  # noqa: ANN001
    ) -> list[ContractRecord]:
        """Extract from guard clauses that validate input.

        Confidence discipline (audit issue #12):
        - Only extract if the exception name passes validation
        - Cap guard-clause-only confidence at 0.65 (below MIN_LIKELY → 'possible')
        - This prevents wrong word extraction from free-text guards
        """
        results: list[ContractRecord] = []
        props = reader.get_properties(node_id, kind="guard_clause")

        # Group guards by the exception they raise
        guards_by_exc: dict[str, list[dict]] = {}
        for p in props:
            value = p.get("value", "")
            exc_type = _extract_validated_exception(value)
            if exc_type:
                guards_by_exc.setdefault(exc_type, []).append(p)

        for exc_type, guards in guards_by_exc.items():
            if len(guards) >= 1:
                # Guard-clause-only: cap at 0.65 (below MIN_LIKELY=0.70 → 'possible')
                # unless multiple independent guards confirm it
                confidence = 0.65 if len(guards) == 1 else 0.75
                results.append(ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=scope_ref,
                    predicate=f"Guard clause raises {exc_type} — must not be removed",
                    normalized_form=f"negative:guard_raise:{exc_type}:{scope_ref}",
                    support_sources=tuple(
                        f"{scope_ref}:{g.get('line', 0)}" for g in guards
                    ),
                    support_count=len(guards),
                    confidence=confidence,
                    tier=tier_from_confidence(confidence, len(guards)),
                ))

        return results


def _extract_validated_exception(value: str) -> str:
    """Extract and validate exception type from a guard clause value.

    Confidence discipline (audit issue #12):
    - Only returns if the extracted name is a recognizable exception type
    - Rejects variable names, partial words, non-exception identifiers
    """
    if not value:
        return ""

    # Look for words that look like exception classes
    for word in value.split():
        cleaned = word.rstrip(",;:()[]")
        if not cleaned or not cleaned[0].isupper():
            continue
        if "Error" in cleaned or "Exception" in cleaned:
            # Reject obvious variable names
            if cleaned in ("ErrorType", "ErrorClass", "ExceptionType"):
                continue
            return cleaned

    return ""


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
