"""ConstructorInvariantExtractor -- mines constructor initialization contracts."""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence

_CTOR_NAMES = {"__init__", "constructor", "new"}
_ATTR_KINDS = {"init_attr", "attr_write"}


class ConstructorInvariantExtractor:
    """Extract conservative constructor invariants."""

    contract_type = "constructor_invariant"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node or not _looks_like_constructor(node):
            return []

        qualified = node.get("qualified_name") or node["name"]
        scope_kind = _label_to_scope(node.get("label", "Method"))
        scope_file = node.get("file_path")
        callers = reader.get_callers(node_id)
        siblings = reader.get_siblings(node_id)
        results: list[ContractRecord] = []

        signature = node.get("signature", "")
        signature_support = len(callers) if callers else len(siblings)
        if signature and signature_support >= 2:
            confidence = 0.95 if signature_support >= 3 else 0.85
            support_sources = tuple(
                f"{c.get('source_file', '')}:{c.get('source_line', 0)}" for c in callers[:5]
            ) or tuple(f"{s.get('file_path', '')}:{s.get('start_line', 0)}" for s in siblings[:5])
            support_kinds = ("callers",) if callers else ("siblings_or_pairs",)
            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate="Constructor signature/init contract must remain compatible",
                    normalized_form=f"constructor_invariant:signature:{qualified}",
                    support_sources=support_sources,
                    support_count=signature_support,
                    confidence=confidence,
                    tier=tier_from_confidence(confidence, signature_support),
                    support_kinds=support_kinds,
                    scope_file=scope_file,
                    checkable=True,
                    freshness_state="unknown",
                )
            )

        for prop in reader.get_properties(node_id, kind=None):
            kind = prop.get("kind", "")
            value = str(prop.get("value", ""))
            if kind == "exception_type" and value:
                results.append(
                    ContractRecord(
                        contract_type=self.contract_type,
                        scope_kind=scope_kind,
                        scope_ref=qualified,
                        predicate=f"Constructor must preserve {value} exception behavior",
                        normalized_form=f"constructor_invariant:exception:{value}:{qualified}",
                        support_sources=(f"{scope_file}:{prop.get('line', 0)}",),
                        support_count=1,
                        confidence=0.80,
                        tier=tier_from_confidence(0.80, 1),
                        support_kinds=("structure",),
                        scope_file=scope_file,
                        checkable=True,
                        freshness_state="unknown",
                    )
                )
            if kind in _ATTR_KINDS and value:
                results.append(
                    ContractRecord(
                        contract_type=self.contract_type,
                        scope_kind=scope_kind,
                        scope_ref=qualified,
                        predicate=f"Constructor must continue initializing attribute {value}",
                        normalized_form=f"constructor_invariant:attr_init:{value}:{qualified}",
                        support_sources=(f"{scope_file}:{prop.get('line', 0)}",),
                        support_count=1,
                        confidence=0.80,
                        tier=tier_from_confidence(0.80, 1),
                        support_kinds=("structure",),
                        scope_file=scope_file,
                        checkable=True,
                        freshness_state="unknown",
                    )
                )

        return results


def _looks_like_constructor(node: dict) -> bool:
    name = node.get("name", "")
    if name in _CTOR_NAMES:
        return True
    qualified = node.get("qualified_name") or ""
    parts = qualified.split(".")
    return len(parts) >= 2 and parts[-1] == parts[-2]


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
