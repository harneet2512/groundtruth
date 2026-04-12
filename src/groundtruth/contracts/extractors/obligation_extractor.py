"""ObligationExtractor — mines behavioral obligations from caller usage patterns.

Migrated from groundtruth_v2/contracts.py. A contract is a behavioral rule
mined from the call graph: if 80%+ of callers share a usage pattern
(destructure, iterate, boolean check, etc.), that pattern IS the contract.

Sources:
1. Caller usage classification from properties table (usage patterns)
2. Signature arity (all callers depend on current param count)
3. Exception contracts (callers that guard with try/catch)
4. Return-shape contracts (callers pass return value to downstream callees)

Confidence model:
- ≥80% caller agreement + ≥3 callers: 0.95 (verified with multi-source)
- ≥80% caller agreement + 2 callers: 0.85 (likely)
- Signature arity with ≥3 callers: 0.95
- Exception with guard callers: 0.85
"""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence

_CONTRACT_THRESHOLD = 0.8  # 80%+ pattern = contract term
_MIN_CALLERS_FOR_PATTERN = 2  # require at least 2 callers


class ObligationExtractor:
    """Extracts behavioral obligation contracts from caller usage patterns."""

    contract_type = "obligation"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        """Extract obligation contracts for a given symbol node."""
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))
        signature = node.get("signature", "")

        # get_callers already filters to confidence >= 0.5 (graph_reader_impl)
        # This prevents name_match cross-file contamination (audit issue #9)
        callers = reader.get_callers(node_id)
        if not callers:
            return []

        results: list[ContractRecord] = []

        # Obligation 1: Signature arity
        if signature and len(callers) >= 2:
            sig_short = signature[:80] if len(signature) > 80 else signature
            confidence = 0.95 if len(callers) >= 3 else 0.85
            support_count = len(callers)
            results.append(ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=qualified,
                predicate=f"Signature must remain compatible: {sig_short}",
                normalized_form=f"obligation:arity:{name}",
                support_sources=tuple(
                    f"{c.get('source_file', '')}:{c.get('source_line', 0)}"
                    for c in callers[:5]
                ),
                support_count=support_count,
                confidence=confidence,
                tier=tier_from_confidence(confidence, support_count),
            ))

        # Obligation 2: Return type usage from caller properties
        usage_counts = self._count_caller_usage(reader, callers, node_id)
        classified = sum(usage_counts.values())

        if classified > 0:
            for pattern, count in sorted(usage_counts.items(), key=lambda x: -x[1]):
                if count < _MIN_CALLERS_FOR_PATTERN:
                    continue
                fraction = count / classified
                if fraction >= _CONTRACT_THRESHOLD:
                    desc = _obligation_from_usage(pattern, count, classified)
                    if desc:
                        confidence = 0.95 if count >= 3 else 0.85
                        results.append(ContractRecord(
                            contract_type=self.contract_type,
                            scope_kind=scope_kind,
                            scope_ref=qualified,
                            predicate=desc,
                            normalized_form=f"obligation:usage:{pattern}:{name}",
                            support_sources=tuple(
                                f"{c.get('source_file', '')}:{c.get('source_line', 0)}"
                                for c in callers[:5]
                            ),
                            support_count=count,
                            confidence=confidence,
                            tier=tier_from_confidence(confidence, count),
                        ))

        # Obligation 3: Exception contract
        exception_contracts = self._extract_exception_obligations(
            reader, node_id, callers, scope_kind, qualified
        )
        results.extend(exception_contracts)

        return results

    def _count_caller_usage(
        self, reader, callers: list[dict], node_id: int  # noqa: ANN001
    ) -> dict[str, int]:
        """Count usage patterns from caller properties.

        Looks at properties with kind containing usage classification:
        destructure_tuple, iterated, boolean_check, exception_guard, etc.
        """
        usage_counts: dict[str, int] = {}

        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue

            # Check properties for usage classification
            props = reader.get_properties(caller_id, kind=None)
            for p in props:
                kind = p.get("kind", "")
                value = p.get("value", "")
                # Usage properties typically have kind like 'return_shape',
                # 'guard_clause', or value containing the usage pattern
                if kind in ("return_shape", "guard_clause"):
                    pattern = kind
                    if "destructure" in value:
                        pattern = "destructure_tuple"
                    elif "iterate" in value or "for" in value:
                        pattern = "iterated"
                    elif "bool" in value or "if" in value:
                        pattern = "boolean_check"
                    elif "except" in value or "catch" in value:
                        pattern = "exception_guard"
                    usage_counts[pattern] = usage_counts.get(pattern, 0) + 1

        return usage_counts

    def _extract_exception_obligations(
        self,
        reader,  # noqa: ANN001
        node_id: int,
        callers: list[dict],
        scope_kind: str,
        scope_ref: str,
    ) -> list[ContractRecord]:
        """Extract exception obligations from properties."""
        results: list[ContractRecord] = []

        # Check exception_type properties on this node
        exc_props = reader.get_properties(node_id, kind="exception_type")
        if not exc_props:
            return results

        # Check if callers have exception_guard usage
        guard_count = 0
        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue
            props = reader.get_properties(caller_id, kind="guard_clause")
            if props:
                guard_count += 1

        if guard_count > 0:
            for prop in exc_props:
                exc_type = prop.get("value", "")
                if not exc_type:
                    continue
                confidence = 0.85 if guard_count >= 2 else 0.75
                results.append(ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=scope_ref,
                    predicate=f"Must continue raising {exc_type}",
                    normalized_form=f"obligation:exception:{exc_type}:{scope_ref}",
                    support_sources=tuple(
                        f"{c.get('source_file', '')}:{c.get('source_line', 0)}"
                        for c in callers[:3] if c.get("source_file")
                    ),
                    support_count=guard_count,
                    confidence=confidence,
                    tier=tier_from_confidence(confidence, guard_count),
                ))

        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")


def _obligation_from_usage(pattern: str, count: int, total: int) -> str | None:
    """Convert a usage pattern into a human-readable obligation."""
    if pattern == "destructure_tuple":
        return f"Return type must remain destructurable ({count}/{total} callers destructure)"
    elif pattern == "iterated":
        return f"Return type must remain iterable ({count}/{total} callers iterate)"
    elif pattern == "boolean_check":
        return f"Return value truthiness must be preserved ({count}/{total} callers use in conditionals)"
    elif pattern == "exception_guard":
        return f"Exception behavior must be preserved ({count}/{total} callers guard with try/catch)"
    return None
