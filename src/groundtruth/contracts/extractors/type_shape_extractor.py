"""TypeShapeExtractor — mines return type/shape contracts.

Split from OutputExtractor (P1.1) to be more precise and verifiable.
Focuses specifically on the SHAPE of the return value rather than
exact literal output.

Sources:
1. Type annotations (return_type field on nodes)
2. Caller destructure patterns (tuple unpacking, dict key access)
3. Sibling return type consistency (>70% agreement)

Confidence model:
- Type annotation: 0.95
- Caller destructure (≥2 callers): 0.85
- Sibling consistency (≥70%): 0.75
"""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence


class TypeShapeExtractor:
    """Extracts type/shape contracts from annotations, callers, and siblings."""

    contract_type = "type_shape"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        """Extract type shape contracts for a given symbol node."""
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))
        return_type = node.get("return_type", "")

        results: list[ContractRecord] = []

        # Source 1: Type annotation (strongest single source)
        if return_type and return_type not in ("None", "void", "", "Any"):
            results.append(ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=qualified,
                predicate=f"Must return {return_type}",
                normalized_form=f"type_shape:{return_type}:{qualified}",
                support_sources=(f"{node.get('file_path', '')}:{node.get('start_line', 0)}",),
                support_count=1,
                confidence=0.95,
                tier=tier_from_confidence(0.95, 1),
            ))

        # Source 2: Caller destructure patterns
        callers = reader.get_callers(node_id)
        destructure_count = 0
        destructure_files: list[str] = []
        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue
            props = reader.get_properties(caller_id, kind="return_shape")
            for p in props:
                value = p.get("value", "")
                if "destructure" in value or "tuple" in value or "unpack" in value:
                    destructure_count += 1
                    destructure_files.append(caller.get("source_file", ""))
                    break

        if destructure_count >= 2:
            confidence = 0.85 if destructure_count >= 3 else 0.75
            results.append(ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=qualified,
                predicate=f"Return must remain destructurable ({destructure_count} callers unpack)",
                normalized_form=f"type_shape:destructurable:{qualified}",
                support_sources=tuple(f"{f}:0" for f in destructure_files[:5]),
                support_count=destructure_count,
                confidence=confidence,
                tier=tier_from_confidence(confidence, destructure_count),
            ))

        # Source 3: Sibling return type consistency
        siblings = reader.get_siblings(node_id)
        if siblings and len(siblings) >= 3:
            sibling_types: dict[str, int] = {}
            for s in siblings:
                rt = s.get("return_type", "")
                if rt:
                    sibling_types[rt] = sibling_types.get(rt, 0) + 1

            if sibling_types:
                most_common_type, count = max(sibling_types.items(), key=lambda x: x[1])
                agreement = count / len(siblings)
                if agreement >= 0.7 and return_type and return_type != most_common_type:
                    # Current return type differs from sibling consensus
                    results.append(ContractRecord(
                        contract_type=self.contract_type,
                        scope_kind=scope_kind,
                        scope_ref=qualified,
                        predicate=f"Siblings return {most_common_type} ({agreement:.0%} agreement) — current: {return_type}",
                        normalized_form=f"type_shape:sibling_mismatch:{most_common_type}:{qualified}",
                        support_sources=tuple(f"sibling:{s.get('name', '')}" for s in siblings[:3]),
                        support_count=count,
                        confidence=0.75,
                        tier=tier_from_confidence(0.75, count),
                    ))

        return results


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
