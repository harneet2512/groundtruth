"""OutputExtractor — mines exact-output/render contracts.

Sources:
1. assertions table: assertEqual(func(x), expected) patterns
2. Caller destructure patterns: tuple unpacking, dict key access
3. nodes.return_type annotation

Confidence model:
- type annotation: 0.95
- test assertEqual: 0.90
- caller destructure: 0.70
"""

from __future__ import annotations

from groundtruth.contracts.types import OutputContract
from groundtruth.substrate.types import ContractRecord, tier_from_confidence


class OutputExtractor:
    """Extracts exact-output/render contracts from the code graph."""

    contract_type = "exact_output"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        """Extract output contracts for a given symbol node."""
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        raw: list[OutputContract] = []

        # Source 1: Return type annotation
        raw.extend(self._from_type_annotation(node))

        # Source 2: Test assertions (assertEqual patterns)
        raw.extend(self._from_assertions(reader, name))

        # Source 3: Caller destructure patterns
        raw.extend(self._from_callers(reader, node_id))

        if not raw:
            return []

        return self._aggregate(raw, scope_kind, qualified)

    def _from_type_annotation(self, node: dict) -> list[OutputContract]:
        """Extract from the node's return_type field."""
        return_type = node.get("return_type")
        if not return_type or return_type in ("None", "void", ""):
            return []

        return [OutputContract(
            return_type=return_type,
            shape_description=_infer_shape(return_type),
            source_file=node.get("file_path", ""),
            source_line=node.get("start_line", 0),
            source_kind="type_annotation",
            confidence=0.95,
        )]

    def _from_assertions(
        self, reader, name: str  # noqa: ANN001
    ) -> list[OutputContract]:
        """Extract from assertEqual-style test assertions."""
        results: list[OutputContract] = []
        assertions = reader.get_assertions_for_target(name)

        for a in assertions:
            kind = a.get("kind", "")
            if kind not in ("assertEqual", "assert_eq", "expect", "assert"):
                continue

            expected = a.get("expected", "")
            _ = a.get("expression", "")  # available for future shape inference

            # Try to infer type from expected value
            return_type = _infer_type_from_expected(expected)
            if not return_type:
                continue

            results.append(OutputContract(
                return_type=return_type,
                shape_description=_infer_shape_from_value(expected),
                source_file=a.get("file_path", a.get("test_name", "")),
                source_line=a.get("line", 0),
                source_kind="test_assertEqual",
                confidence=0.90,
            ))

        return results

    def _from_callers(
        self, reader, node_id: int  # noqa: ANN001
    ) -> list[OutputContract]:
        """Extract from caller destructure patterns.

        If callers unpack the return value (a, b = func()) this implies
        a tuple return contract.
        """
        results: list[OutputContract] = []
        callers = reader.get_callers(node_id)

        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue

            # Check for return_shape properties on the caller that reference
            # destructuring of our node's output
            props = reader.get_properties(caller_id, kind="return_shape")
            for p in props:
                value = p.get("value", "")
                if not value:
                    continue
                results.append(OutputContract(
                    return_type=value,
                    shape_description=value,
                    source_file=caller.get("source_file", ""),
                    source_line=p.get("line", 0),
                    source_kind="caller_destructure",
                    confidence=0.70,
                ))

        return results

    def _aggregate(
        self,
        raw: list[OutputContract],
        scope_kind: str,
        scope_ref: str,
    ) -> list[ContractRecord]:
        """Group by return_type, compute confidence and tier."""
        # Group by normalized return type
        groups: dict[str, list[OutputContract]] = {}
        for contract in raw:
            key = contract.return_type.strip()
            groups.setdefault(key, []).append(contract)

        results: list[ContractRecord] = []
        for return_type, items in groups.items():
            sources = set()
            for item in items:
                sources.add((item.source_file, item.source_kind))

            support_count = len(sources)
            max_confidence = max(item.confidence for item in items)
            if support_count >= 2:
                max_confidence = min(1.0, max_confidence + 0.05)

            tier = tier_from_confidence(max_confidence, support_count)

            # Use shape_description from highest-confidence source
            shape = max(items, key=lambda x: x.confidence).shape_description

            predicate = f"returns {return_type}"
            if shape:
                predicate += f" ({shape})"
            normalized = f"returns:{return_type}"

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
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")


def _infer_shape(return_type: str) -> str:
    """Infer shape from a type annotation string."""
    rt = return_type.strip()
    if rt.startswith("tuple[") or rt.startswith("Tuple["):
        # Count items: tuple[int, str] → 'tuple(2)'
        inner = rt.split("[", 1)[1].rstrip("]")
        count = len([x for x in inner.split(",") if x.strip()])
        return f"tuple({count})"
    if rt.startswith("list[") or rt.startswith("List["):
        inner = rt.split("[", 1)[1].rstrip("]")
        return f"list[{inner}]"
    if rt.startswith("dict[") or rt.startswith("Dict["):
        return "dict"
    if rt.startswith("Optional["):
        inner = rt[9:].rstrip("]")
        return f"optional({inner})"
    return ""


def _infer_shape_from_value(expected: str) -> str:
    """Infer shape from a literal expected value."""
    expected = expected.strip()
    if expected.startswith("(") and expected.endswith(")"):
        # Tuple literal
        items = expected[1:-1].split(",")
        return f"tuple({len(items)})"
    if expected.startswith("[") and expected.endswith("]"):
        return "list"
    if expected.startswith("{") and expected.endswith("}"):
        return "dict"
    return ""


def _infer_type_from_expected(expected: str) -> str:
    """Infer return type from an expected value literal."""
    expected = expected.strip()
    if not expected:
        return ""
    if expected.startswith("(") and expected.endswith(")"):
        return "tuple"
    if expected.startswith("[") and expected.endswith("]"):
        return "list"
    if expected.startswith("{") and expected.endswith("}"):
        return "dict"
    if expected in ("True", "False"):
        return "bool"
    if expected == "None":
        return ""  # None return is not interesting as an output contract
    try:
        int(expected)
        return "int"
    except ValueError:
        pass
    try:
        float(expected)
        return "float"
    except ValueError:
        pass
    if expected.startswith(("'", '"')):
        return "str"
    return ""
