"""
P14 Serialization Pair Detection -- Edge Case Tests

Tests the detectSerializationPairs logic proposed in BUILD_RESEARCH_P6_P14.md.
Since P14 is not yet implemented, this file IMPLEMENTS the spec locally and then
tests edge cases against it.

Edge cases tested:
8.  Function named "dump" with no "load" counterpart in same file -> should NOT create pair
9.  Multiple to_json functions in different files -> should only pair within same file/parent
10. Case-sensitive: "to_json" and "TO_JSON" -> should NOT pair (different names in Go maps)
"""

import sys
from dataclasses import dataclass, field


# ── P14 Implementation (from BUILD_RESEARCH_P6_P14.md spec, Approach A) ──

# Serialization pair patterns from the spec
SERDE_PAIRS: list[tuple[str, str]] = [
    ("to_json", "from_json"),
    ("to_dict", "from_dict"),
    ("to_yaml", "from_yaml"),
    ("to_xml", "from_xml"),
    ("to_csv", "from_csv"),
    ("to_string", "from_string"),
    ("to_bytes", "from_bytes"),
    ("serialize", "deserialize"),
    ("marshal", "unmarshal"),
    ("encode", "decode"),
    ("pack", "unpack"),
    ("dump", "load"),
    ("dumps", "loads"),
    ("write", "read"),
    ("save", "load"),
    ("export", "import_"),
    ("to_proto", "from_proto"),
    ("to_msgpack", "from_msgpack"),
    ("toJSON", "fromJSON"),
    ("toObject", "fromObject"),
    ("toString", "fromString"),
    ("ToJSON", "FromJSON"),
    ("Marshal", "Unmarshal"),
    ("Encode", "Decode"),
    ("MarshalJSON", "UnmarshalJSON"),
    ("MarshalText", "UnmarshalText"),
]


@dataclass
class FunctionNode:
    """Simplified representation of a function node from graph.db."""
    id: int
    name: str
    file_path: str
    parent_id: int = 0  # 0 = top-level, >0 = inside a class
    start_line: int = 0


@dataclass
class SerializationPair:
    """A detected pair of serialization/deserialization functions."""
    serializer: FunctionNode
    deserializer: FunctionNode
    pattern: tuple[str, str]


@dataclass
class UnpairedFunction:
    """A function matching one side of a serde pattern but missing its counterpart."""
    function: FunctionNode
    missing_counterpart: str
    pattern: tuple[str, str]


def detect_serialization_pairs(
    functions: list[FunctionNode],
) -> tuple[list[SerializationPair], list[UnpairedFunction]]:
    """
    Implements the P14 spec: scan function nodes for matching serde pairs.

    Pairing rules (from spec):
    1. Both sides must exist in the SAME file or SAME parent (class)
    2. Matching is EXACT name match (Go map lookup, case-sensitive)
    3. If only one side exists, record as unpaired (potential bug)
    """
    pairs: list[SerializationPair] = []
    unpaired: list[UnpairedFunction] = []

    # Build index: (file_path, parent_id) -> {name: FunctionNode}
    # This groups functions by their "scope" (same file + same class)
    scope_index: dict[tuple[str, int], dict[str, FunctionNode]] = {}
    for fn in functions:
        scope_key = (fn.file_path, fn.parent_id)
        if scope_key not in scope_index:
            scope_index[scope_key] = {}
        scope_index[scope_key][fn.name] = fn

    # For each scope, check all serde pair patterns
    for scope_key, scope_funcs in scope_index.items():
        for pattern in SERDE_PAIRS:
            ser_name, deser_name = pattern
            has_ser = ser_name in scope_funcs
            has_deser = deser_name in scope_funcs

            if has_ser and has_deser:
                pairs.append(SerializationPair(
                    serializer=scope_funcs[ser_name],
                    deserializer=scope_funcs[deser_name],
                    pattern=pattern,
                ))
            elif has_ser and not has_deser:
                unpaired.append(UnpairedFunction(
                    function=scope_funcs[ser_name],
                    missing_counterpart=deser_name,
                    pattern=pattern,
                ))
            elif has_deser and not has_ser:
                unpaired.append(UnpairedFunction(
                    function=scope_funcs[deser_name],
                    missing_counterpart=ser_name,
                    pattern=pattern,
                ))

    return pairs, unpaired


# ── Tests ────────────────────────────────────────────────────────────────


def test_dump_without_load_no_pair():
    """Edge case 8: Function named 'dump' with no 'load' counterpart in same file
    should NOT create a pair. It should appear as UnpairedFunction instead."""
    functions = [
        FunctionNode(id=1, name="dump", file_path="serializer.py", parent_id=0, start_line=10),
        FunctionNode(id=2, name="format_output", file_path="serializer.py", parent_id=0, start_line=20),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    # No pair should be created
    assert len(pairs) == 0, (
        f"Expected 0 pairs (dump has no load counterpart), got {len(pairs)}: "
        f"{[(p.serializer.name, p.deserializer.name) for p in pairs]}"
    )

    # 'dump' should appear in unpaired list
    dump_unpaired = [u for u in unpaired if u.function.name == "dump" and u.missing_counterpart == "load"]
    assert len(dump_unpaired) >= 1, (
        f"Expected 'dump' to appear in unpaired (missing 'load'), got unpaired: "
        f"{[(u.function.name, u.missing_counterpart) for u in unpaired]}"
    )

    print("  PASS: test_dump_without_load_no_pair")


def test_dump_with_load_creates_pair():
    """Positive control: dump + load in same file SHOULD create a pair."""
    functions = [
        FunctionNode(id=1, name="dump", file_path="serializer.py", parent_id=0, start_line=10),
        FunctionNode(id=2, name="load", file_path="serializer.py", parent_id=0, start_line=30),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    dump_load_pairs = [p for p in pairs if p.pattern == ("dump", "load")]
    assert len(dump_load_pairs) == 1, (
        f"Expected exactly 1 dump/load pair in same file, got {len(dump_load_pairs)}"
    )

    # Note: "save"/"load" pattern also exists. "load" matches both ("dump","load")
    # and ("save","load"). Without "save", "save"/"load" should be unpaired.
    # The "load" function matches the deser side of ("save","load"), so "save" is
    # recorded as missing.
    save_unpaired = [u for u in unpaired if u.missing_counterpart == "save"]
    # This is expected -- "load" exists but "save" doesn't
    assert len(save_unpaired) >= 1, (
        "Expected 'load' to create unpaired entry for missing 'save'"
    )

    print("  PASS: test_dump_with_load_creates_pair")


def test_to_json_cross_file_no_pair():
    """Edge case 9: Multiple to_json functions in different files should only pair
    within same file/parent. to_json in file A and from_json in file B should
    NOT create a pair."""
    functions = [
        FunctionNode(id=1, name="to_json", file_path="models/user.py", parent_id=10, start_line=15),
        FunctionNode(id=2, name="from_json", file_path="models/order.py", parent_id=20, start_line=25),
        # Same class in user.py: this SHOULD pair
        FunctionNode(id=3, name="from_json", file_path="models/user.py", parent_id=10, start_line=30),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    # Only the user.py pair (same file + same parent_id=10) should match
    valid_pairs = [
        p for p in pairs
        if p.pattern == ("to_json", "from_json")
    ]
    assert len(valid_pairs) == 1, (
        f"Expected exactly 1 to_json/from_json pair (same scope), got {len(valid_pairs)}"
    )
    assert valid_pairs[0].serializer.file_path == "models/user.py"
    assert valid_pairs[0].deserializer.file_path == "models/user.py"
    assert valid_pairs[0].serializer.parent_id == valid_pairs[0].deserializer.parent_id

    # order.py's from_json should be unpaired (no to_json in same scope)
    order_unpaired = [
        u for u in unpaired
        if u.function.file_path == "models/order.py"
        and u.function.name == "from_json"
        and u.missing_counterpart == "to_json"
    ]
    assert len(order_unpaired) >= 1, (
        f"Expected order.py's from_json to be unpaired, got: "
        f"{[(u.function.file_path, u.function.name, u.missing_counterpart) for u in unpaired]}"
    )

    print("  PASS: test_to_json_cross_file_no_pair")


def test_case_sensitive_no_pair():
    """Edge case 10: 'to_json' and 'TO_JSON' should NOT pair because Go maps are
    case-sensitive. The serde pairs list has exact entries ('to_json', 'from_json')
    and ('ToJSON', 'FromJSON'). 'TO_JSON' matches neither pattern."""
    functions = [
        FunctionNode(id=1, name="to_json", file_path="model.py", parent_id=0, start_line=10),
        FunctionNode(id=2, name="TO_JSON", file_path="model.py", parent_id=0, start_line=20),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    # "to_json" should be unpaired (looking for "from_json" which doesn't exist)
    # "TO_JSON" doesn't match ANY pattern side, so it shouldn't appear at all
    to_json_pairs = [p for p in pairs if p.pattern == ("to_json", "from_json")]
    assert len(to_json_pairs) == 0, (
        f"Expected 0 pairs: 'to_json' and 'TO_JSON' are different names. Got: "
        f"{[(p.serializer.name, p.deserializer.name) for p in to_json_pairs]}"
    )

    # "to_json" should be in unpaired, missing "from_json"
    to_json_unpaired = [
        u for u in unpaired
        if u.function.name == "to_json" and u.missing_counterpart == "from_json"
    ]
    assert len(to_json_unpaired) == 1, (
        f"Expected 'to_json' unpaired (missing 'from_json'), got: "
        f"{[(u.function.name, u.missing_counterpart) for u in unpaired]}"
    )

    # "TO_JSON" should NOT appear in any pair or unpaired entry as a known pattern side
    all_func_names_in_results = set()
    for p in pairs:
        all_func_names_in_results.add(p.serializer.name)
        all_func_names_in_results.add(p.deserializer.name)
    for u in unpaired:
        all_func_names_in_results.add(u.function.name)

    # TO_JSON should NOT be in results as a matched function since it doesn't
    # match any pattern exactly
    to_json_upper_in_results = [
        u for u in unpaired if u.function.name == "TO_JSON"
    ]
    assert len(to_json_upper_in_results) == 0, (
        f"'TO_JSON' should not match any serde pattern (case-sensitive). Got: "
        f"{[(u.function.name, u.missing_counterpart) for u in to_json_upper_in_results]}"
    )

    print("  PASS: test_case_sensitive_no_pair")


def test_same_file_different_class_no_pair():
    """Functions in same file but different classes should NOT pair."""
    functions = [
        FunctionNode(id=1, name="serialize", file_path="models.py", parent_id=10, start_line=15),
        FunctionNode(id=2, name="deserialize", file_path="models.py", parent_id=20, start_line=50),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    ser_deser_pairs = [p for p in pairs if p.pattern == ("serialize", "deserialize")]
    assert len(ser_deser_pairs) == 0, (
        f"Expected 0 pairs (different parent classes), got {len(ser_deser_pairs)}"
    )

    print("  PASS: test_same_file_different_class_no_pair")


def test_same_class_creates_pair():
    """Positive control: same file + same parent class SHOULD create pair."""
    functions = [
        FunctionNode(id=1, name="serialize", file_path="models.py", parent_id=10, start_line=15),
        FunctionNode(id=2, name="deserialize", file_path="models.py", parent_id=10, start_line=50),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    ser_deser_pairs = [p for p in pairs if p.pattern == ("serialize", "deserialize")]
    assert len(ser_deser_pairs) == 1, (
        f"Expected 1 pair (same class), got {len(ser_deser_pairs)}"
    )

    print("  PASS: test_same_class_creates_pair")


def test_go_naming_conventions():
    """Go uses ToJSON/FromJSON and Marshal/Unmarshal -- should pair correctly."""
    functions = [
        FunctionNode(id=1, name="ToJSON", file_path="model.go", parent_id=5, start_line=10),
        FunctionNode(id=2, name="FromJSON", file_path="model.go", parent_id=5, start_line=30),
        FunctionNode(id=3, name="MarshalJSON", file_path="model.go", parent_id=5, start_line=50),
        FunctionNode(id=4, name="UnmarshalJSON", file_path="model.go", parent_id=5, start_line=70),
    ]

    pairs, unpaired = detect_serialization_pairs(functions)

    tojson_pairs = [p for p in pairs if p.pattern == ("ToJSON", "FromJSON")]
    marshal_pairs = [p for p in pairs if p.pattern == ("MarshalJSON", "UnmarshalJSON")]

    assert len(tojson_pairs) == 1, f"Expected ToJSON/FromJSON pair, got {len(tojson_pairs)}"
    assert len(marshal_pairs) == 1, f"Expected MarshalJSON/UnmarshalJSON pair, got {len(marshal_pairs)}"

    print("  PASS: test_go_naming_conventions")


def main():
    print("=" * 60)
    print("P14 Serialization Pair Detection -- Edge Case Tests")
    print("=" * 60)

    tests = [
        test_dump_without_load_no_pair,
        test_dump_with_load_creates_pair,
        test_to_json_cross_file_no_pair,
        test_case_sensitive_no_pair,
        test_same_file_different_class_no_pair,
        test_same_class_creates_pair,
        test_go_naming_conventions,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
