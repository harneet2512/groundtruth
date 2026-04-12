"""ExceptionExtractor — mines exception/message contracts.

Sources:
1. assertions table: assertRaises / pytest.raises targeting this node
2. properties table: kind='exception_type' on the node itself (guard clauses)
3. Caller catch patterns: callers that wrap this call in try/except

Confidence model:
- test assertion (assertRaises): 0.95
- guard clause in function body: 0.90
- caller catch pattern: 0.70
"""

from __future__ import annotations

from groundtruth.contracts.types import ExceptionContract
from groundtruth.substrate.types import ContractRecord, tier_from_confidence


class ExceptionExtractor:
    """Extracts exception/message contracts from the code graph."""

    contract_type = "exception_message"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        """Extract exception contracts for a given symbol node."""
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        # Collect raw evidence from all three sources
        raw: list[ExceptionContract] = []

        # Source 1: Test assertions (assertRaises / pytest.raises)
        raw.extend(self._from_assertions(reader, name))

        # Source 2: Guard clauses in the function body
        raw.extend(self._from_properties(reader, node_id))

        # Source 3: Caller catch patterns
        raw.extend(self._from_caller_catches(reader, node_id))

        if not raw:
            return []

        # Group by exception_type and aggregate
        return self._aggregate(raw, scope_kind, qualified)

    def _from_assertions(
        self, reader, name: str  # noqa: ANN001
    ) -> list[ExceptionContract]:
        """Extract from test assertions targeting this symbol."""
        results: list[ExceptionContract] = []
        assertions = reader.get_assertions_for_target(name)

        for a in assertions:
            kind = a.get("kind", "")
            if kind not in ("assertRaises", "raises", "assert_raises"):
                continue

            # Extract exception type from expression
            expr = a.get("expression", "")
            exc_type = _parse_exception_from_assertion(expr, a.get("expected", ""))
            if not exc_type:
                continue

            msg = _parse_message_from_assertion(expr)
            results.append(ExceptionContract(
                exception_type=exc_type,
                message_pattern=msg,
                source_file=a.get("file_path", a.get("test_name", "")),
                source_line=a.get("line", 0),
                source_kind="test_assertion",
                confidence=0.95,
            ))

        return results

    def _from_properties(
        self, reader, node_id: int  # noqa: ANN001
    ) -> list[ExceptionContract]:
        """Extract from guard clause / raise properties on this node."""
        results: list[ExceptionContract] = []

        # Check exception_type properties
        props = reader.get_properties(node_id, kind="exception_type")
        for p in props:
            value = p.get("value", "")
            if not value:
                continue
            results.append(ExceptionContract(
                exception_type=value,
                message_pattern="",
                source_file="",  # Same file as the node
                source_line=p.get("line", 0),
                source_kind="guard_clause",
                confidence=0.90,
            ))

        # Also check raise_type properties
        raise_props = reader.get_properties(node_id, kind="raise_type")
        for p in raise_props:
            value = p.get("value", "")
            if not value:
                continue
            results.append(ExceptionContract(
                exception_type=value,
                message_pattern="",
                source_file="",
                source_line=p.get("line", 0),
                source_kind="guard_clause",
                confidence=0.90,
            ))

        return results

    def _from_caller_catches(
        self, reader, node_id: int  # noqa: ANN001
    ) -> list[ExceptionContract]:
        """Extract from callers that catch exceptions from this function."""
        results: list[ExceptionContract] = []
        callers = reader.get_callers(node_id)

        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue

            # Check if caller has exception_guard properties that reference
            # exception types (indicating it catches exceptions from callees)
            props = reader.get_properties(caller_id, kind="guard_clause")
            for p in props:
                value = p.get("value", "")
                # Guard clause values might be like "except ValueError" or "catch(IOError)"
                exc_type = _parse_exception_from_guard(value)
                if exc_type:
                    results.append(ExceptionContract(
                        exception_type=exc_type,
                        message_pattern="",
                        source_file=caller.get("source_file", ""),
                        source_line=p.get("line", 0),
                        source_kind="caller_catch",
                        confidence=0.70,
                    ))

        return results

    def _aggregate(
        self,
        raw: list[ExceptionContract],
        scope_kind: str,
        scope_ref: str,
    ) -> list[ContractRecord]:
        """Group by exception_type, compute confidence and tier."""
        # Group by exception_type
        groups: dict[str, list[ExceptionContract]] = {}
        for contract in raw:
            key = contract.exception_type
            groups.setdefault(key, []).append(contract)

        results: list[ContractRecord] = []
        for exc_type, items in groups.items():
            # Compute support: count unique (source_file, source_kind) pairs
            sources = set()
            for item in items:
                sources.add((item.source_file, item.source_kind))

            support_count = len(sources)
            # Overall confidence: max of individual confidences
            max_confidence = max(item.confidence for item in items)
            # Boost confidence if multiple independent sources
            if support_count >= 2:
                max_confidence = min(1.0, max_confidence + 0.05)

            tier = tier_from_confidence(max_confidence, support_count)

            # Build predicate and normalized form
            msg = next((i.message_pattern for i in items if i.message_pattern), "")
            if msg:
                predicate = f"raises {exc_type}('{msg}')"
                normalized = f"raises:{exc_type}:{msg}"
            else:
                predicate = f"raises {exc_type}"
                normalized = f"raises:{exc_type}"

            # Build support_sources tuple
            support_sources = tuple(
                f"{item.source_file}:{item.source_line}"
                for item in items
                if item.source_file
            )

            results.append(ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                predicate=predicate,
                normalized_form=normalized,
                support_sources=support_sources,
                support_count=support_count,
                confidence=max_confidence,
                tier=tier,
            ))

        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _label_to_scope(label: str) -> str:
    """Convert node label to scope_kind."""
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")


def _parse_exception_from_assertion(expression: str, expected: str) -> str:
    """Extract exception type from an assertion expression.

    Examples:
        'assertRaises(ValueError, func, args)' → 'ValueError'
        'pytest.raises(KeyError)' → 'KeyError'
        expected='TypeError' → 'TypeError'
    """
    if expected and expected[0].isupper():
        return expected.split("(")[0].strip()

    # Try parsing from expression
    for pattern_start in ("assertRaises(", "raises(", "assert_raises("):
        if pattern_start in expression:
            rest = expression.split(pattern_start, 1)[1]
            exc = rest.split(",")[0].split(")")[0].strip()
            if exc and exc[0].isupper():
                return exc

    return ""


def _parse_message_from_assertion(expression: str) -> str:
    """Extract expected message from assertion if available."""
    # Look for string literals in the expression
    for quote in ('"', "'"):
        if quote in expression:
            parts = expression.split(quote)
            if len(parts) >= 3:
                return parts[1]
    return ""


def _parse_exception_from_guard(value: str) -> str:
    """Extract exception type from a guard clause value.

    Examples:
        'except ValueError' → 'ValueError'
        'catch(IOError)' → 'IOError'
        'ValueError' → 'ValueError'
    """
    value = value.strip()
    if value.startswith("except "):
        value = value[7:].strip()
    elif value.startswith("catch("):
        value = value[6:].rstrip(")")

    # Take first word (might be 'ValueError as e')
    exc = value.split()[0] if value else ""
    if exc and exc[0].isupper():
        return exc
    return ""
