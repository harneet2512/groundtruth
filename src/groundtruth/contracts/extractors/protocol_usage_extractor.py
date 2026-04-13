"""ProtocolUsageExtractor -- mines caller-driven usage contracts."""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence

_MIN_SUPPORT = 2
_THRESHOLD = 0.8


class ProtocolUsageExtractor:
    """Extract concrete protocol-usage contracts from caller properties."""

    contract_type = "protocol_usage"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        callers = reader.get_callers(node_id)
        if len(callers) < _MIN_SUPPORT:
            return []

        qualified = node.get("qualified_name") or node["name"]
        scope_kind = _label_to_scope(node.get("label", "Function"))
        counts: dict[str, list[str]] = {}

        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue
            for usage in _usages_for_caller(reader, caller_id, node["name"]):
                counts.setdefault(usage, []).append(
                    f"{caller.get('source_file', '')}:{caller.get('source_line', 0)}"
                )

        total = len(callers)
        results: list[ContractRecord] = []
        for usage, sources in counts.items():
            support_count = len(sources)
            if support_count < _MIN_SUPPORT:
                continue
            if (support_count / total) < _THRESHOLD:
                continue
            confidence = 0.95 if support_count >= 3 else 0.85
            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=_predicate_for(usage, support_count, total),
                    normalized_form=f"protocol_usage:{usage}:{qualified}",
                    support_sources=tuple(sources[:5]),
                    support_count=support_count,
                    confidence=confidence,
                    tier=tier_from_confidence(confidence, support_count),
                    support_kinds=("callers",),
                    scope_file=node.get("file_path"),
                    checkable=True,
                    freshness_state="unknown",
                )
            )
        return results


def _usages_for_caller(reader, caller_id: int, target_name: str) -> set[str]:  # noqa: ANN001
    results: set[str] = set()
    for prop in reader.get_properties(caller_id, kind=None):
        kind = prop.get("kind", "")
        value = str(prop.get("value", ""))
        if kind == "caller_usage":
            usage, _, callee = value.partition(":")
            if callee and callee != target_name:
                continue
            normalized = _normalize_usage(usage)
            if normalized:
                results.add(normalized)
        elif kind == "return_shape":
            lowered = value.lower()
            if any(token in lowered for token in ("destructure", "tuple", "unpack")):
                results.add("destructurable")
            if "iterat" in lowered or "for " in lowered:
                results.add("iterable")
            if "attr" in lowered or "property" in lowered or "." in lowered:
                results.add("attr_access")
            if "bool" in lowered or "if " in lowered:
                results.add("truthy")
    return results


def _normalize_usage(usage: str) -> str | None:
    mapping = {
        "destructure_tuple": "destructurable",
        "destructure_list": "destructurable",
        "iterated": "iterable",
        "boolean_check": "truthy",
        "attr_access": "attr_access",
        "method_call": "attr_access",
        "index_access": "iterable",
        "len_call": "iterable",
    }
    return mapping.get(usage)


def _predicate_for(usage: str, count: int, total: int) -> str:
    if usage == "destructurable":
        return f"Return protocol must remain destructurable ({count}/{total} callers unpack it)"
    if usage == "iterable":
        return f"Return protocol must remain iterable ({count}/{total} callers iterate or size-check it)"
    if usage == "attr_access":
        return f"Return protocol must preserve attribute/member access ({count}/{total} callers dereference it)"
    if usage == "truthy":
        return f"Return protocol must preserve truthiness ({count}/{total} callers branch on it)"
    return f"Return protocol must preserve {usage} ({count}/{total} callers)"


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
