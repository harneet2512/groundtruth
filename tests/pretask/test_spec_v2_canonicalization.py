"""RED-first contracts for deterministic obligation canonicalization."""
from __future__ import annotations

import json

from groundtruth.pretask.spec import extract_spec_v2


def test_equivalent_normative_variants_merge_without_losing_sources() -> None:
    issue = """### Expected behavior
`parse_item` should preserve empty values.
`parse_item` must preserve empty values.
"""

    rows = extract_spec_v2(issue).obligations

    assert len(rows) == 1
    assert rows[0].modality == "mandatory"
    assert rows[0].modality_strength == 3
    assert rows[0].source_variants == (
        "`parse_item` should preserve empty values",
        "`parse_item` must preserve empty values",
    )


def test_canonicalization_never_merges_polarity_or_predicate_changes() -> None:
    issue = """### Requirements
`parse_item` must preserve empty values.
`parse_item` must not preserve empty values.
`parse_item` must reject empty values.
`parse_item` must preserve insertion order.
"""

    rows = extract_spec_v2(issue).obligations

    assert len(rows) == 4
    assert all(len(row.source_variants) == 1 for row in rows)


def test_explicit_adjacent_fence_is_additive_verification_guidance() -> None:
    issue = """### Expected behavior
`parse_item` must preserve empty values.

For example:
```python
assert parse_item({"x": []}) == {"x": []}
```
"""

    rows = extract_spec_v2(issue).obligations

    assert len(rows) == 1
    assert rows[0].verbatim_text == "`parse_item` must preserve empty values"
    assert len(rows[0].verification_cases) == 1
    case = rows[0].verification_cases[0]
    assert case.introduced_by == "For example:"
    assert case.language == "python"
    assert case.verbatim_text == 'assert parse_item({"x": []}) == {"x": []}'


def test_unintroduced_or_nonadjacent_fences_are_not_attached() -> None:
    issue = """### Expected behavior
`parse_item` must preserve empty values.

```python
assert parse_item(None) is None
```

For example:
### Notes
```python
assert parse_item(0) == 0
```
"""

    rows = extract_spec_v2(issue).obligations

    assert len(rows) == 1
    assert rows[0].verification_cases == ()


def test_canonical_serialization_is_deterministic_and_keeps_guidance() -> None:
    issue = """### Expected behavior
`decode_item` should return an empty mapping.
`decode_item` must return an empty mapping.

Example:
```json
{}
```
"""

    first = extract_spec_v2(issue).to_serializable(version=2)
    second = extract_spec_v2(issue).to_serializable(version=2)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first[0]["source_variants"] == [
        "`decode_item` should return an empty mapping",
        "`decode_item` must return an empty mapping",
    ]
    assert first[0]["verification_cases"] == [
        {
            "verbatim_text": "{}",
            "language": "json",
            "introduced_by": "Example:",
        }
    ]


def test_limit_is_applied_after_canonicalization_not_during_collection() -> None:
    issue = """### Requirements
`parse_item` should preserve empty values.
`parse_item` must preserve empty values.
`decode_item` must return an empty mapping.
"""

    rows = extract_spec_v2(issue, max_obligations=2).obligations

    assert [row.subject_symbols for row in rows] == [
        frozenset({"parse_item"}),
        frozenset({"decode_item"}),
    ]


def test_union_views_are_rebuilt_from_final_canonical_limited_rows() -> None:
    issue = """### Requirements
`parse_item` should preserve empty values.
`parse_item` must preserve empty values.
`decode_item` must return an empty mapping.
"""

    spec = extract_spec_v2(issue, max_obligations=1)
    expected_symbols = set().union(*(
        set(row.symbols) | set(row.subject_symbols) for row in spec.obligations
    ))
    expected_keywords = set().union(*(
        set(row.keywords) for row in spec.obligations
    ))

    assert spec.all_symbols == expected_symbols
    assert spec.all_keywords == expected_keywords
    assert "decode_item" not in spec.all_symbols
    assert "should" not in spec.all_keywords
