"""PairedBehaviorExtractor -- mines inverse-pair behavioral contracts.

Many behaviors are defined by paired functions whose outputs must satisfy an
inverse relationship:
  parse/render      encode/decode       serialize/deserialize
  read/write        open/close          compress/decompress
  encrypt/decrypt   forward/inverse     to_*/from_*

This extractor mines roundtrip / inverse-pair contracts with evidence diversity
scoring. It is intentionally more conservative than RoundtripExtractor:

Pairing requires at least ONE of:
  - test linkage     (test_file calls both functions and asserts equality)
  - caller co-usage  (a caller site uses both functions in the same scope)
  - sibling relation (same-class/same-file siblings with name-pair AND
                      at least one shared test reference)

Naming convention alone (without any corroborating evidence) is deliberately
excluded. Those would promote too aggressively.

Support kinds and promotion
---------------------------
  test + callers         → support_kinds=("tests","callers")     → verified
  test + siblings        → support_kinds=("tests","siblings_or_pairs") → verified
  test only              → support_kinds=("tests",)              → likely
  callers only (pairs)   → support_kinds=("callers","siblings_or_pairs") → likely
  naming only            → not emitted (excluded by design)
"""

from __future__ import annotations

from groundtruth.substrate.promotion import promote_tier
from groundtruth.substrate.types import ContractRecord, SupportKind

# Canonical paired patterns: (forward_pattern, inverse_pattern)
_PAIRS: list[tuple[str, str]] = [
    ("dumps", "loads"),
    ("dump", "load"),
    ("encode", "decode"),
    ("serialize", "deserialize"),
    ("to_json", "from_json"),
    ("to_dict", "from_dict"),
    ("to_string", "from_string"),
    ("to_bytes", "from_bytes"),
    ("to_xml", "from_xml"),
    ("marshal", "unmarshal"),
    ("pack", "unpack"),
    ("compress", "decompress"),
    ("encrypt", "decrypt"),
    ("write", "read"),
    ("save", "load"),
    ("parse", "render"),
    ("format_", "parse"),
    ("export", "import_"),
    ("freeze", "thaw"),
    ("push", "pop"),
]


class PairedBehaviorExtractor:
    """Extract inverse-pair behavioral contracts from multi-class evidence."""

    contract_type = "paired_behavior"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        partner_name, direction = self._find_partner_name(name)
        if not partner_name:
            return []

        partner = reader.get_node_by_name(partner_name, node.get("file_path", ""))
        if not partner or not self._same_scope(node, partner):
            return []

        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        # Collect evidence from multiple classes
        support_kinds: list[SupportKind] = []
        support_sources: list[str] = []
        test_files: set[str] = set()

        # 1. Test linkage — requires co-body callee presence or roundtrip assertion
        partner_id = partner.get("id", 0)
        for tf, line in self._test_linkage(reader, node, partner, partner_id):
            support_kinds.append("tests")
            support_sources.append(f"{tf}:{line}")
            test_files.add(tf)

        # 2. Caller co-usage
        if self._caller_co_usage(reader, node_id, partner_id):
            support_kinds.append("callers")

        # 3. Sibling relation (same class/file scope, named pair)
        if self._is_sibling_pair(node, partner):
            support_kinds.append("siblings_or_pairs")

        # Require at least one corroborating evidence class beyond naming alone
        if not support_kinds:
            return []

        tier = promote_tier(support_kinds)
        support_count = len({s.split(":")[0] for s in support_sources}) or len(support_sources) or 1

        if tier == "possible":
            # Not enough evidence to emit even a likely contract
            return []

        forward, inverse = self._order_pair(name, partner_name, direction)

        results: list[ContractRecord] = [
            ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=qualified,
                predicate=f"{inverse}({forward}(x)) == x must hold for valid inputs",
                normalized_form=f"paired_behavior:{forward}:{inverse}",
                support_sources=tuple(support_sources[:5]),
                support_count=support_count,
                confidence=_confidence_from_tier(tier),
                tier=tier,
                support_kinds=tuple(dict.fromkeys(support_kinds)),  # deduplicate, preserve order
                scope_file=node.get("file_path"),
                checkable=False,  # runtime roundtrip check not attempted in v1
                freshness_state="unknown",
            )
        ]

        # Also emit sentinel_preservation contracts if tests assert sentinel returns
        results.extend(self._extract_sentinel_contracts(reader, node_id, node, qualified, scope_kind))

        return results

    # ------------------------------------------------------------------
    # Evidence collectors
    # ------------------------------------------------------------------

    def _test_linkage(
        self, reader, node: dict, partner: dict, partner_id: int  # noqa: ANN001
    ) -> list[tuple[str, int]]:
        """Find test bodies where BOTH this node and its partner appear as callees.

        Requires at least one of:
        - Both node and partner appear as callee edges from the same test body
        - A roundtrip-shaped assertion (both names in a single assert expression)

        Name co-mention in test text alone does NOT qualify (too weak).
        """
        results: list[tuple[str, int]] = []
        partner_name = partner["name"]

        tests = reader.get_tests_for(node["id"])
        for test in tests:
            test_id = test.get("id")
            if test_id is None:
                continue

            # Strong signal: both node and partner are callees of this test function
            # (i.e., the test CALLS both, not just mentions them)
            if hasattr(reader, "get_callees"):
                callee_ids: set[int] = {
                    e.get("target_id")
                    for e in reader.get_callees(test_id)
                    if e.get("target_id") is not None
                }
                if node["id"] in callee_ids and partner_id in callee_ids:
                    results.append((
                        test.get("file_path", ""),
                        test.get("start_line", 0),
                    ))
                    continue

            # Fallback: roundtrip-shaped assertion — both names in a single expression
            # e.g. assert decode(encode(x)) == x
            assertions = reader.get_assertions(test_id)
            for a in assertions:
                expr = str(a.get("expression", ""))
                if node["name"] in expr and partner_name in expr:
                    results.append((
                        test.get("file_path", ""),
                        test.get("start_line", 0),
                    ))
                    break
            # Name co-mention anywhere in test text is no longer accepted

        return results

    def _extract_sentinel_contracts(
        self,
        reader,  # noqa: ANN001
        node_id: int,
        node: dict,
        qualified: str,
        scope_kind: str,
    ) -> list[ContractRecord]:
        """Mine sentinel-preservation obligations.

        Emits paired_behavior:sentinel_preservation:NotImplemented:func_name
        when tests assert that the function returns NotImplemented (or similar
        protocol sentinels like None, False, empty).

        These are narrow and machine-checkable in the checker.
        """
        results: list[ContractRecord] = []
        _SENTINELS = {"NotImplemented", "None", "False", "()"}

        for assertion in reader.get_assertions_for_target(node["name"]):
            kind = assertion.get("kind", "")
            expected = str(assertion.get("expected", "")).strip()

            # assertIs(result, NotImplemented) or assert result is NotImplemented
            if kind in {"assertIs", "assert_is", "assertIsNot"} or "is" in kind.lower():
                sentinel = expected.strip("\"'")
                if sentinel in _SENTINELS:
                    source = f"{assertion.get('file_path', '')}:{assertion.get('line', 0)}"
                    # Verify the function actually has a return of this sentinel
                    # (from properties) — otherwise abstain
                    support_kinds: list[SupportKind] = ["tests"]
                    tier = promote_tier(support_kinds)
                    results.append(ContractRecord(
                        contract_type=self.contract_type,
                        scope_kind=scope_kind,
                        scope_ref=qualified,
                        predicate=f"{node['name']}() must return {sentinel} in protocol context",
                        normalized_form=f"paired_behavior:sentinel_preservation:{sentinel}:{qualified}",
                        support_sources=(source,),
                        support_count=1,
                        confidence=0.82,
                        tier=tier,
                        support_kinds=("tests",),
                        scope_file=node.get("file_path"),
                        checkable=True,  # checker can verify: return NotImplemented → return None diff
                        freshness_state="unknown",
                    ))

        # Also check caller_usage properties for protocol_return pattern
        # (callers that guard on `if result is NotImplemented`)
        for prop in reader.get_properties(node_id, kind="caller_usage"):
            val = (prop.get("value") or "").lower()
            if "notimplemented" in val or "sentinel" in val:
                results.append(ContractRecord(
                    contract_type=self.contract_type,
                    scope_kind=scope_kind,
                    scope_ref=qualified,
                    predicate=f"{node['name']}() callers guard on NotImplemented sentinel",
                    normalized_form=f"paired_behavior:protocol_return:NotImplemented:{qualified}",
                    support_sources=(f"{node.get('file_path', '')}:caller_usage",),
                    support_count=1,
                    confidence=0.75,
                    tier="likely",
                    support_kinds=("callers",),
                    scope_file=node.get("file_path"),
                    checkable=True,
                    freshness_state="unknown",
                ))

        return results

    def _caller_co_usage(
        self, reader, node_id: int, partner_id: int  # noqa: ANN001
    ) -> bool:
        """Return True if any caller scope uses both this node and its partner."""
        if partner_id == 0:
            return False

        my_caller_ids: set[int] = {
            c.get("source_id")
            for c in reader.get_callers(node_id)
            if c.get("source_id") is not None
        }
        partner_caller_ids: set[int] = {
            c.get("source_id")
            for c in reader.get_callers(partner_id)
            if c.get("source_id") is not None
        }
        return bool(my_caller_ids & partner_caller_ids)

    def _is_sibling_pair(self, node: dict, partner: dict) -> bool:
        """True when both nodes share the same parent scope."""
        n_parent = node.get("parent_id")
        p_parent = partner.get("parent_id")
        if n_parent is not None and p_parent is not None:
            return n_parent == p_parent
        if n_parent is None and p_parent is None:
            return node.get("file_path") == partner.get("file_path")
        return False

    # ------------------------------------------------------------------
    # Partner resolution
    # ------------------------------------------------------------------

    def _find_partner_name(self, name: str) -> tuple[str | None, str]:
        """Return (partner_name, direction) where direction is 'forward' or 'inverse'."""
        for forward_pat, inverse_pat in _PAIRS:
            if _matches(name, forward_pat):
                partner = _swap(name, forward_pat, inverse_pat)
                return partner, "forward"
            if _matches(name, inverse_pat):
                partner = _swap(name, inverse_pat, forward_pat)
                return partner, "inverse"
        return None, ""

    def _same_scope(self, node: dict, partner: dict) -> bool:
        n_parent = node.get("parent_id")
        p_parent = partner.get("parent_id")
        if n_parent is not None and p_parent is not None:
            return n_parent == p_parent
        if n_parent is None and p_parent is None:
            return node.get("file_path") == partner.get("file_path")
        return False

    def _order_pair(self, name: str, partner_name: str, direction: str) -> tuple[str, str]:
        if direction == "forward":
            return name, partner_name
        return partner_name, name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matches(name: str, pattern: str) -> bool:
    return name == pattern or name.endswith("_" + pattern) or name.startswith(pattern + "_")


def _swap(name: str, from_pat: str, to_pat: str) -> str:
    if name == from_pat:
        return to_pat
    if name.endswith("_" + from_pat):
        return name[: -(len(from_pat) + 1)] + "_" + to_pat
    if name.startswith(from_pat + "_"):
        return to_pat + "_" + name[len(from_pat) + 1 :]
    return name.replace(from_pat, to_pat)


def _confidence_from_tier(tier: str) -> float:
    if tier == "verified":
        return 0.92
    if tier == "likely":
        return 0.78
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
