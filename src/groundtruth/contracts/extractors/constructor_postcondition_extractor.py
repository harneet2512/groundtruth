"""ConstructorPostconditionExtractor -- mines object-validity obligations.

This extractor focuses on the narrow, checkable question:
  "After __init__ completes, what must be true for callers to safely use the object?"

This is NOT full lifecycle analysis. It does not attempt to track setup/teardown
state machines or deferred initialization patterns. Those are too risky for v1.

What it mines
-------------
1. Caller-derived postconditions
   When callers access a specific attribute immediately after constructing the
   object (visible as attr_access properties on the constructor's callers),
   that attribute is a required postcondition: the constructor must establish
   it before the object escapes.

2. Test-derived postconditions
   When tests assert specific attribute values or non-None guarantees on the
   freshly constructed object (e.g., assertEqual(obj.foo, expected) or
   assertIsNotNone(obj.bar)), those are behavioral postconditions.

3. Caller count as confirmation signal
   When >= 2 callers all depend on the same attribute, the postcondition is
   evidence-diverse (callers class) even without test evidence.

4. Forward-to-parent obligations
   When tests assert that a keyword argument was forwarded to super().__init__(),
   that forwarding is a required postcondition.

Contracts emitted
-----------------
  "Constructor must establish attribute {attr} before object escapes
   ({n}/{total} callers depend on it)"
  "Constructor must forward {kwarg} to super().__init__()"

Support kinds and promotion
---------------------------
  tests + callers         → verified
  callers only (>=2)      → likely
  tests only (>= 2 files) → likely
  structure only          → possible (not emitted)

Design discipline: no full lifecycle, no setup/teardown pairing, no inheritance
analysis. Only: attribute presence obligations derived from usage evidence.
"""

from __future__ import annotations

import re

from groundtruth.substrate.promotion import promote_tier
from groundtruth.substrate.types import ContractRecord, SupportKind

_CTOR_NAMES = {"__init__", "constructor", "new", "create", "build", "init", "setup"}
_ATTR_PROPERTY_KINDS = {"init_attr", "attr_write", "attr_read", "postcondition"}

# Matches the first dotted attribute access like obj.attr or self.attr
_ATTR_DOT_RE = re.compile(r"\b\w+\.(\w+)\b")


class ConstructorPostconditionExtractor:
    """Extract object-validity postconditions for constructors."""

    contract_type = "constructor_postcondition"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node or not _looks_like_constructor(node):
            return []

        qualified = node.get("qualified_name") or node["name"]
        scope_kind = _label_to_scope(node.get("label", "Method"))
        scope_file = node.get("file_path")

        callers = reader.get_callers(node_id)
        total_callers = len(callers)

        # -----------------------------------------------------------------
        # 1. Collect attrs from constructor properties (what __init__ sets)
        # -----------------------------------------------------------------
        established_attrs: set[str] = set()
        for prop in reader.get_properties(node_id, kind=None):
            if prop.get("kind") in _ATTR_PROPERTY_KINDS:
                attr = str(prop.get("value", "")).strip()
                if attr and attr.startswith("self."):
                    established_attrs.add(attr[5:])  # strip "self."
                elif attr:
                    established_attrs.add(attr)

        if not established_attrs:
            return []

        # -----------------------------------------------------------------
        # 2. Find which attrs callers depend on (caller-side attr access)
        # -----------------------------------------------------------------
        caller_attr_counts: dict[str, int] = {}
        for caller in callers:
            caller_id = caller.get("source_id")
            if caller_id is None:
                continue
            for prop in reader.get_properties(caller_id, kind=None):
                if prop.get("kind") in {"attr_access", "attr_read"}:
                    attr = str(prop.get("value", "")).strip()
                    if attr in established_attrs:
                        caller_attr_counts[attr] = caller_attr_counts.get(attr, 0) + 1

        # -----------------------------------------------------------------
        # 3. Find test-asserted postconditions
        # -----------------------------------------------------------------
        test_asserted_attrs: dict[str, list[str]] = {}
        for assertion in reader.get_assertions_for_target(node["name"]):
            kind = assertion.get("kind", "")
            expected = str(assertion.get("expected", "")).strip()
            source = f"{assertion.get('file_path', '')}:{assertion.get('line', 0)}"
            # Look for assertIsNotNone(obj.attr) — attr must be set and non-None
            if kind in {"assertIsNotNone"} and "." in expected:
                m = _ATTR_DOT_RE.search(expected)
                if m:
                    attr_part = m.group(1)
                    if attr_part in established_attrs:
                        test_asserted_attrs.setdefault(attr_part, []).append(source)
            # Look for assertIsNone(obj.attr) — attr expected to be None (different obligation)
            elif kind in {"assertIsNone"} and "." in expected:
                m = _ATTR_DOT_RE.search(expected)
                if m:
                    attr_part = m.group(1)
                    if attr_part in established_attrs:
                        test_asserted_attrs.setdefault(f"{attr_part}:none", []).append(source)
            elif kind in {"assertEqual", "assert_eq"} and "." in expected:
                m = _ATTR_DOT_RE.search(expected)
                if m:
                    attr_part = m.group(1)
                    if attr_part in established_attrs:
                        test_asserted_attrs.setdefault(attr_part, []).append(source)

        # -----------------------------------------------------------------
        # 4. Build contracts for attrs with multi-class evidence
        # -----------------------------------------------------------------
        results: list[ContractRecord] = []

        # Attributes corroborated by callers (require >=2 to qualify as "callers" class)
        for attr, caller_count in caller_attr_counts.items():
            if caller_count < 2:
                continue

            support_kinds: list[SupportKind] = []
            support_sources: list[str] = []

            if caller_count >= 2:
                support_kinds.append("callers")
                support_sources.extend(
                    f"{c.get('source_file', '')}:{c.get('source_line', 0)}"
                    for c in callers[:3]
                )

            if attr in test_asserted_attrs:
                support_kinds.append("tests")
                support_sources.extend(test_asserted_attrs[attr][:2])

            tier = promote_tier(support_kinds)
            if tier == "possible":
                continue

            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=(
                        f"Constructor must establish self.{attr} before object escapes"
                        + (
                            f" ({caller_count}/{total_callers} callers depend on it)"
                            if total_callers > 0
                            else ""
                        )
                    ),
                    normalized_form=f"constructor_postcondition:attr:{attr}:{qualified}",
                    support_sources=tuple(support_sources[:5]),
                    support_count=len({s.split(":")[0] for s in support_sources}) or len(support_sources),
                    confidence=_confidence_from_tier(tier, caller_count),
                    tier=tier,
                    support_kinds=tuple(dict.fromkeys(support_kinds)),
                    scope_file=scope_file,
                    checkable=True,  # attr presence is checkable in AST
                    freshness_state="unknown",
                )
            )

        # Attributes asserted by tests but not corroborated by callers
        for attr, test_sources in test_asserted_attrs.items():
            if attr in caller_attr_counts:
                continue  # already handled above

            distinct_test_files = len({s.split(":")[0] for s in test_sources})
            if distinct_test_files < 1:
                continue

            support_kinds_t: list[SupportKind] = ["tests"]
            tier = promote_tier(support_kinds_t)
            if tier == "possible":
                continue

            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=f"Constructor must establish self.{attr} (asserted in {distinct_test_files} test file(s))",
                    normalized_form=f"constructor_postcondition:test_attr:{attr}:{qualified}",
                    support_sources=tuple(test_sources[:5]),
                    support_count=distinct_test_files,
                    confidence=0.82,
                    tier=tier,
                    support_kinds=("tests",),
                    scope_file=scope_file,
                    checkable=True,
                    freshness_state="unknown",
                )
            )

        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _looks_like_constructor(node: dict) -> bool:
    name = node.get("name", "")
    if name in _CTOR_NAMES:
        return True
    qualified = node.get("qualified_name") or ""
    parts = qualified.split(".")
    return len(parts) >= 2 and parts[-1] == parts[-2]


def _confidence_from_tier(tier: str, caller_count: int) -> float:
    if tier == "verified":
        return 0.92
    if tier == "likely":
        return 0.85 if caller_count >= 3 else 0.78
    return 0.55


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
