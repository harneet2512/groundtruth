"""ExactRenderStringExtractor -- mines exact string/render contracts."""

from __future__ import annotations

from groundtruth.substrate.types import ContractRecord, tier_from_confidence

_ASSERT_KINDS = {"assertEqual", "assert_eq", "expect", "assert", "assertIn"}


class ExactRenderStringExtractor:
    """Extract exact string contracts from explicit test expectations."""

    contract_type = "exact_render_string"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))
        groups: dict[str, list[str]] = {}

        for assertion in reader.get_assertions_for_target(name):
            kind = assertion.get("kind", "")
            expected = str(assertion.get("expected", "")).strip()
            if kind not in _ASSERT_KINDS:
                continue
            string_value = _extract_string_literal(expected)
            if not string_value:
                continue
            source = f"{assertion.get('file_path', assertion.get('test_name', ''))}:{assertion.get('line', 0)}"
            groups.setdefault(string_value, []).append(source)

        results: list[ContractRecord] = []
        for expected, sources in groups.items():
            support_count = len({src.split(":")[0] for src in sources}) or len(sources)
            confidence = 0.95 if support_count >= 2 else 0.90
            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=f"Rendered/output string must preserve {expected!r}",
                    normalized_form=f"exact_render_string:{qualified}:{expected}",
                    support_sources=tuple(sources[:5]),
                    support_count=support_count,
                    confidence=confidence,
                    tier=tier_from_confidence(confidence, support_count),
                    support_kinds=("tests",),
                    scope_file=node.get("file_path"),
                    checkable=True,
                    freshness_state="unknown",
                )
            )

        return results


def _extract_string_literal(expected: str) -> str | None:
    if len(expected) >= 2 and expected[0] == expected[-1] and expected[0] in {"'", '"'}:
        return expected[1:-1]
    return None


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
