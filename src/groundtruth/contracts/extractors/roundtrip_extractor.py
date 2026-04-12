"""RoundtripExtractor — mines roundtrip/serialization contracts.

Detects test patterns where encode(decode(x)) == x or equivalent:
- dumps/loads
- serialize/deserialize
- encode/decode
- to_json/from_json
- to_dict/from_dict

Sources:
1. Tests that call both encode+decode and assert equality
2. Parser/serializer pair detection via naming conventions

Confidence model:
- Direct test evidence with assertion: 0.95
- Naming convention match without test: 0.60
"""

from __future__ import annotations

from groundtruth.contracts.types import RoundtripContract
from groundtruth.substrate.types import ContractRecord, tier_from_confidence


# Roundtrip pair patterns: (encode_pattern, decode_pattern)
_ROUNDTRIP_PAIRS: list[tuple[str, str]] = [
    ("dumps", "loads"),
    ("dump", "load"),
    ("encode", "decode"),
    ("serialize", "deserialize"),
    ("to_json", "from_json"),
    ("to_dict", "from_dict"),
    ("to_string", "from_string"),
    ("to_bytes", "from_bytes"),
    ("marshal", "unmarshal"),
    ("pack", "unpack"),
    ("compress", "decompress"),
    ("encrypt", "decrypt"),
    ("write", "read"),
    ("export", "import_"),
    ("save", "load"),
]


class RoundtripExtractor:
    """Extracts roundtrip/serialization contracts from test code."""

    contract_type = "roundtrip"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        """Extract roundtrip contracts for a given symbol node."""
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        raw: list[RoundtripContract] = []

        # Check if this node is one half of a roundtrip pair
        partner = self._find_partner(reader, node)
        if not partner:
            return []

        # Look for tests that call both this node and its partner
        raw.extend(self._from_test_evidence(reader, node, partner))

        if not raw:
            # Naming convention match only (lower confidence)
            return self._from_naming_convention(node, partner, scope_kind, qualified)

        return self._aggregate(raw, scope_kind, qualified)

    def _find_partner(self, reader, node: dict) -> dict | None:  # noqa: ANN001
        """Find the roundtrip partner for this node.

        Resolution correctness (audit issue #4):
        - Partner must be in the same scope (same parent_id = same class)
        - If parent_id differs, it's a naming coincidence, not a pair
        """
        name = node["name"]
        file_path = node.get("file_path", "")
        parent_id = node.get("parent_id")

        for encode_pat, decode_pat in _ROUNDTRIP_PAIRS:
            if name.endswith(encode_pat) or name == encode_pat:
                partner_name = name.replace(encode_pat, decode_pat)
                partner = reader.get_node_by_name(partner_name, file_path)
                if partner and self._same_scope(node, partner):
                    return partner
            elif name.endswith(decode_pat) or name == decode_pat:
                partner_name = name.replace(decode_pat, encode_pat)
                partner = reader.get_node_by_name(partner_name, file_path)
                if partner and self._same_scope(node, partner):
                    return partner

        return None

    def _same_scope(self, node: dict, partner: dict) -> bool:
        """Check if two nodes are in the same scope (class or file).

        Two nodes are in the same scope if:
        - Same parent_id (same class), OR
        - Both have no parent (module-level, same file)
        """
        n_parent = node.get("parent_id")
        p_parent = partner.get("parent_id")
        if n_parent is not None and p_parent is not None:
            return n_parent == p_parent
        # Both module-level: check same file
        if n_parent is None and p_parent is None:
            return node.get("file_path") == partner.get("file_path")
        return False

    def _from_test_evidence(
        self, reader, node: dict, partner: dict  # noqa: ANN001
    ) -> list[RoundtripContract]:
        """Find tests that exercise both sides of the roundtrip."""
        results: list[RoundtripContract] = []
        node_name = node["name"]
        partner_name = partner["name"]

        # Get tests for this node
        tests = reader.get_tests_for(node["id"])

        for test in tests:
            test_id = test["id"]
            # Check if this test also references the partner
            assertions = reader.get_assertions(test_id)
            test_text = " ".join(
                a.get("expression", "") + " " + a.get("expected", "")
                for a in assertions
            )

            if partner_name in test_text or partner_name in test.get("name", ""):
                # Determine which is encode and which is decode
                encode_sym, decode_sym = self._order_pair(node_name, partner_name)
                results.append(RoundtripContract(
                    encode_symbol=encode_sym,
                    decode_symbol=decode_sym,
                    test_file=test.get("file_path", ""),
                    test_line=test.get("start_line", 0),
                    confidence=0.95,
                ))

        return results

    def _from_naming_convention(
        self,
        node: dict,
        partner: dict,
        scope_kind: str,
        scope_ref: str,
    ) -> list[ContractRecord]:
        """Create a lower-confidence contract from naming convention alone."""
        encode_sym, decode_sym = self._order_pair(node["name"], partner["name"])

        return [ContractRecord(
            contract_type=self.contract_type,
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            predicate=f"{decode_sym}({encode_sym}(x)) == x",
            normalized_form=f"roundtrip:{encode_sym}:{decode_sym}",
            support_sources=(f"{node.get('file_path', '')}:{node.get('start_line', 0)}",),
            support_count=1,
            confidence=0.50,
            tier="possible",  # Naming-only: suppress from runtime (P0.3)
        )]

    def _aggregate(
        self,
        raw: list[RoundtripContract],
        scope_kind: str,
        scope_ref: str,
    ) -> list[ContractRecord]:
        """Convert raw roundtrip evidence to ContractRecords."""
        # Group by (encode, decode) pair
        groups: dict[tuple[str, str], list[RoundtripContract]] = {}
        for contract in raw:
            key = (contract.encode_symbol, contract.decode_symbol)
            groups.setdefault(key, []).append(contract)

        results: list[ContractRecord] = []
        for (encode_sym, decode_sym), items in groups.items():
            support_count = len(items)
            max_confidence = max(item.confidence for item in items)
            if support_count >= 2:
                max_confidence = min(1.0, max_confidence + 0.05)

            tier = tier_from_confidence(max_confidence, support_count)

            support_sources = tuple(
                f"{item.test_file}:{item.test_line}"
                for item in items
                if item.test_file
            )

            results.append(ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                predicate=f"{decode_sym}({encode_sym}(x)) == x",
                normalized_form=f"roundtrip:{encode_sym}:{decode_sym}",
                support_sources=support_sources,
                support_count=support_count,
                confidence=max_confidence,
                tier=tier,
            ))

        return results

    def _order_pair(self, name_a: str, name_b: str) -> tuple[str, str]:
        """Determine which is encode and which is decode."""
        for encode_pat, _ in _ROUNDTRIP_PAIRS:
            if encode_pat in name_a:
                return name_a, name_b
            if encode_pat in name_b:
                return name_b, name_a
        # Default: alphabetical order
        return (name_a, name_b) if name_a < name_b else (name_b, name_a)


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
