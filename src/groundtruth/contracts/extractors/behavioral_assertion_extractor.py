"""BehavioralAssertionExtractor -- mines behavioral obligations from test assertions.

This is the strongest general source of behavioral truth. Test assertions express
intended behavioral contracts directly: what must be returned, what must be raised,
what must be contained in the output.

Mined assertion kinds
---------------------
assertEqual / assert_eq / ==      exact value obligation
assertRaises / pytest.raises      exception contract
assertIn / in                     containment obligation
assertNotIn / not in              exclusion contract
assertTrue / assertFalse          truthiness contract
assertIsNone / assertIsNotNone    nullability contract
assert (bare)                     general truthy expectation

Confidence and promotion
------------------------
Support comes from tests only by definition, so:
- support_kinds = ("tests",)
- verified requires >= 2 distinct test files (tests class reaches score 3, but
  needs one more class for >= 4; tests alone => "likely"). Only when callers
  ALSO confirm the same expectation do we reach "verified".
- The extractor therefore defaults to "likely" and marks contracts that appear
  in >= 2 test files at higher confidence for ranking purposes.

Design discipline: only statically recognizable assertions. No arbitrary dynamic
test logic. The contract text must be short and checkable.
"""

from __future__ import annotations

from groundtruth.substrate.promotion import promote_tier
from groundtruth.substrate.types import ContractRecord

# Assertion kinds that carry exact value obligations
_EXACT_VALUE_KINDS = {"assertEqual", "assert_eq", "assert_equal"}
# Assertion kinds that express exception obligations
_EXCEPTION_KINDS = {"assertRaises", "pytest.raises", "raises"}
# Assertion kinds that express containment
_CONTAINS_KINDS = {"assertIn", "assertContains"}
# Assertion kinds that express exclusion
_EXCLUDES_KINDS = {"assertNotIn", "assertNotContains"}
# Assertion kinds that express truthiness
_TRUTHINESS_KINDS = {"assertTrue", "assertFalse", "assert_true", "assert_false"}
# Assertion kinds that express nullability
_NULL_KINDS = {"assertIsNone", "assertIsNotNone", "assert_is_none", "assert_is_not_none"}

_MAX_EXPECTED_LEN = 120  # Truncate very long expected values


class BehavioralAssertionExtractor:
    """Extract behavioral contracts from test assertion evidence."""

    contract_type = "behavioral_assertion"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        # Group assertions by (obligation_class, normalized_expectation)
        groups: dict[tuple[str, str], list[str]] = {}

        for assertion in reader.get_assertions_for_target(name):
            kind = assertion.get("kind", "")
            expected = str(assertion.get("expected", "")).strip()
            source_file = assertion.get("file_path", assertion.get("test_name", ""))
            source_line = assertion.get("line", 0)
            source = f"{source_file}:{source_line}"

            obligation = _classify_assertion(kind, expected)
            if obligation is None:
                continue

            key = obligation
            groups.setdefault(key, []).append(source)

        results: list[ContractRecord] = []
        for (obligation_class, expected_summary), sources in groups.items():
            distinct_files = {s.split(":")[0] for s in sources}
            support_count = len(distinct_files) or len(sources)

            # Confidence scales with how many distinct test files confirm this
            if support_count >= 3:
                confidence = 0.95
            elif support_count >= 2:
                confidence = 0.90
            else:
                confidence = 0.80

            support_kinds = ("tests",)
            tier = promote_tier(support_kinds)

            predicate = _build_predicate(obligation_class, expected_summary, name)
            normalized = f"behavioral_assertion:{obligation_class}:{qualified}:{expected_summary[:60]}"

            results.append(
                ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=predicate,
                    normalized_form=normalized,
                    support_sources=tuple(sources[:5]),
                    support_count=support_count,
                    confidence=confidence,
                    tier=tier,
                    support_kinds=support_kinds,
                    scope_file=node.get("file_path"),
                    checkable=_is_checkable(obligation_class),
                    freshness_state="unknown",
                )
            )

        # ── Property-based extraction (fallback when no test assertions exist) ──
        # Mine behavioral contracts from structural properties the Go indexer
        # produces: return_shape, exception_type, caller_usage.
        #
        # These produce "likely" contracts when caller evidence also exists,
        # "possible" otherwise (possible is skipped by the checker).
        results.extend(_extract_from_properties(reader, node_id, qualified, scope_kind, node))

        return results


def _extract_from_properties(
    reader,  # noqa: ANN001
    node_id: int,
    qualified: str,
    scope_kind: str,
    node: dict,
) -> list[ContractRecord]:
    """Mine behavioral contracts from structural properties.

    Uses: return_shape, exception_type, caller_usage properties.
    Requires caller evidence to reach 'likely' tier (structure alone = possible,
    which the checker skips).
    """
    contracts: list[ContractRecord]= []
    file_path = node.get("file_path") or ""
    name = node["name"]

    # Collect caller evidence: call-graph edges OR caller_usage properties
    # (caller_usage properties appear when callers exist in the source but were
    # not captured as edges — e.g. NumPy ufuncs called from C internals).
    callers = reader.get_callers(node_id) if hasattr(reader, "get_callers") else []
    caller_usage_props = reader.get_properties(node_id, kind="caller_usage")
    has_callers = bool(callers) or bool(caller_usage_props)

    # ── return_shape: value ──
    # If a function has return_shape=value AND callers, it means callers depend on
    # a non-None return. Contract: must not add 'return None'.
    return_shapes = reader.get_properties(node_id, kind="return_shape")
    has_value_return = any(p.get("value") == "value" for p in return_shapes)
    has_none_return = any(p.get("value") == "none" for p in return_shapes)

    if has_value_return and not has_none_return and has_callers:
        support_kinds = ("structure", "callers")
        tier = promote_tier(support_kinds)  # structure(1) + callers(2) = 3 → "likely"
        contracts.append(ContractRecord(
            contract_type="behavioral_assertion",
            scope_kind=scope_kind,
            scope_ref=qualified,
            predicate=f"{name}() must return a value (not None); callers rely on non-None result",
            normalized_form=f"behavioral_assertion:nullability:not_none:{qualified}",
            support_sources=(f"{file_path}:return_shape",),
            support_count=len(callers),
            confidence=0.80,
            tier=tier,
            support_kinds=support_kinds,
            scope_file=file_path,
            checkable=True,
            freshness_state="unknown",
        ))

    # ── exception_type ──
    # If the function has documented exception types AND guard_clause raise evidence,
    # it signals that callers may depend on those exceptions being raised.
    exc_props = reader.get_properties(node_id, kind="exception_type")
    guard_props = reader.get_properties(node_id, kind="guard_clause")
    has_raise_guard = any("raise" in (p.get("value") or "") for p in guard_props)

    for exc_prop in exc_props:
        exc_name = (exc_prop.get("value") or "").strip()
        if not exc_name or exc_name.startswith("e"):
            continue  # Skip bare 'e' (unparsed) or empty
        if has_callers and has_raise_guard:
            support_kinds = ("structure", "callers")
            tier = promote_tier(support_kinds)
        else:
            support_kinds = ("structure",)
            tier = promote_tier(support_kinds)  # possible — checker skips, but recorded
        contracts.append(ContractRecord(
            contract_type="behavioral_assertion",
            scope_kind=scope_kind,
            scope_ref=qualified,
            predicate=f"{name}() must raise {exc_name} in documented error conditions",
            normalized_form=f"behavioral_assertion:raises_exception:{exc_name}:{qualified}",
            support_sources=(f"{file_path}:exception_type",),
            support_count=1,
            confidence=0.70,
            tier=tier,
            support_kinds=support_kinds,
            scope_file=file_path,
            checkable=True,
            freshness_state="unknown",
        ))

    return contracts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_assertion(kind: str, expected: str) -> tuple[str, str] | None:
    """Return (obligation_class, normalized_expectation) or None to skip."""
    if kind in _EXACT_VALUE_KINDS:
        summary = expected[:_MAX_EXPECTED_LEN] if expected else "(empty)"
        return ("exact_value", summary)

    if kind in _EXCEPTION_KINDS:
        exc_name = expected.strip("\"'") or "Exception"
        return ("raises_exception", exc_name)

    if kind in _CONTAINS_KINDS:
        summary = expected[:_MAX_EXPECTED_LEN] if expected else "(value)"
        return ("output_contains", summary)

    if kind in _EXCLUDES_KINDS:
        summary = expected[:_MAX_EXPECTED_LEN] if expected else "(value)"
        return ("output_excludes", summary)

    if kind in _TRUTHINESS_KINDS:
        polarity = "falsy" if "False" in kind or "false" in kind else "truthy"
        return ("truthiness", polarity)

    if kind in _NULL_KINDS:
        polarity = "not_none" if "NotNone" in kind or "not_none" in kind else "none"
        return ("nullability", polarity)

    if kind in {"assert", "assertThat"}:
        # Bare assert: just record that a positive assertion exists
        summary = expected[:60] if expected else "(truthy)"
        return ("truthy_assertion", summary)

    return None


def _build_predicate(
    obligation_class: str, expected_summary: str, func_name: str
) -> str:
    if obligation_class == "exact_value":
        return f"{func_name}() must return {expected_summary!r}"
    if obligation_class == "raises_exception":
        return f"{func_name}() must raise {expected_summary}"
    if obligation_class == "output_contains":
        return f"{func_name}() output must contain {expected_summary!r}"
    if obligation_class == "output_excludes":
        return f"{func_name}() output must not contain {expected_summary!r}"
    if obligation_class == "truthiness":
        return f"{func_name}() result must be {expected_summary}"
    if obligation_class == "nullability":
        return (
            f"{func_name}() must return None"
            if expected_summary == "none"
            else f"{func_name}() must not return None"
        )
    return f"{func_name}() must satisfy: {expected_summary}"


def _is_checkable(obligation_class: str) -> bool:
    """Whether the verifier can machine-check this obligation class."""
    # Exception and null contracts are easiest to check structurally
    return obligation_class in {
        "raises_exception",
        "nullability",
        "exact_value",
        "output_contains",
    }


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
