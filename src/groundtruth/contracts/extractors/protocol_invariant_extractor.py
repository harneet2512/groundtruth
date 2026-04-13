"""ProtocolInvariantExtractor -- mines language-agnostic caller protocol contracts.

This family promotes caller usage facts into explicit contracts:
- destructurable
- iterable
- attribute-bearing
- truthiness-preserving

All signals come from graph properties/caller usage rather than Python AST.
"""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence

_MIN_SUPPORT = 2
_THRESHOLD = 0.8


class ProtocolInvariantExtractor:
    """Extracts protocol invariants from caller usage patterns."""

    contract_type = "protocol_invariant"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        callers = reader.get_callers(node_id)
        if len(callers) < _MIN_SUPPORT:
            return []

        counts: dict[str, list[str]] = {}
        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue
            for pattern in _patterns_for_caller(reader, caller_id, node["name"]):
                counts.setdefault(pattern, []).append(
                    f"{caller.get('source_file', '')}:{caller.get('source_line', 0)}"
                )

        qualified = node.get("qualified_name") or node["name"]
        scope_kind = _label_to_scope(node.get("label", "Function"))
        total = len(callers)
        results: list[ContractRecord] = []

        for pattern, sources in counts.items():
            support_count = len(sources)
            if support_count < _MIN_SUPPORT:
                continue
            agreement = support_count / total
            if agreement < _THRESHOLD:
                continue
            confidence = 0.95 if support_count >= 3 else 0.85
            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=_predicate(pattern, support_count, total),
                    normalized_form=f"protocol_invariant:{pattern}:{qualified}",
                    support_sources=tuple(sources[:5]),
                    support_count=support_count,
                    confidence=confidence,
                    tier=tier_from_confidence(confidence, support_count),
                )
            )

        return results


def _patterns_for_caller(reader, caller_id: int, target_name: str) -> set[str]:  # noqa: ANN001
    patterns: set[str] = set()
    for prop in reader.get_properties(caller_id, kind=None):
        kind = prop.get("kind", "")
        value = prop.get("value", "")
        if kind == "caller_usage":
            usage, _, callee = value.partition(":")
            if callee and callee != target_name:
                continue
            if usage in {"destructure_tuple", "destructure_list", "iterated", "boolean_check", "attr_access"}:
                patterns.add(_normalize_usage(usage))
        elif kind == "return_shape":
            lowered = value.lower()
            if any(token in lowered for token in ("destructure", "tuple", "unpack")):
                patterns.add("destructurable")
            if "iterat" in lowered or "for " in lowered:
                patterns.add("iterable")
            if "attr" in lowered or "property" in lowered or "." in lowered:
                patterns.add("attr_access")
            if "bool" in lowered or "if " in lowered:
                patterns.add("truthy")
    return patterns


def _normalize_usage(usage: str) -> str:
    mapping = {
        "destructure_tuple": "destructurable",
        "destructure_list": "destructurable",
        "iterated": "iterable",
        "boolean_check": "truthy",
        "attr_access": "attr_access",
    }
    return mapping.get(usage, usage)


def _predicate(pattern: str, count: int, total: int) -> str:
    if pattern == "destructurable":
        return f"Return must remain destructurable ({count}/{total} callers unpack it)"
    if pattern == "iterable":
        return f"Return must remain iterable ({count}/{total} callers iterate it)"
    if pattern == "attr_access":
        return f"Return must preserve attribute access semantics ({count}/{total} callers access attributes)"
    if pattern == "truthy":
        return f"Return truthiness must be preserved ({count}/{total} callers use it in conditionals)"
    return f"Protocol invariant {pattern} must be preserved ({count}/{total} callers)"


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
