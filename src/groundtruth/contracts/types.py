"""Contract-specific internal types for extraction.

These types are used during the extraction pipeline. The final output
is always ContractRecord (from substrate.types) — these are intermediate
representations specific to each extractor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExceptionContract:
    """Exception/message contract: function raises X with message Y.

    Extracted from:
    - assertRaises / pytest.raises in tests
    - guard clauses that raise in the function body
    - caller catch/except patterns
    """

    exception_type: str
    """e.g. 'ValueError', 'KeyError', 'TypeError'"""

    message_pattern: str
    """e.g. 'x must be positive', '' if message unknown"""

    source_file: str
    """File where this evidence was found."""

    source_line: int
    """Line number of the evidence."""

    source_kind: str
    """How found: 'test_assertion' | 'guard_clause' | 'caller_catch'"""

    confidence: float
    """0.0-1.0: test_assertion=0.95, guard_clause=0.9, caller_catch=0.7"""


@dataclass(frozen=True)
class OutputContract:
    """Exact-output/render contract: function returns specific shape.

    Extracted from:
    - assertEqual(func(x), expected) in tests
    - Caller destructure patterns (tuple unpacking, dict key access)
    - Return type annotations
    """

    return_type: str
    """e.g. 'Optional[User]', 'tuple[int, str]', 'dict'"""

    shape_description: str
    """e.g. 'tuple(2)' | 'dict{keys:a,b,c}' | 'list[str]' | ''"""

    source_file: str
    source_line: int

    source_kind: str
    """'test_assertEqual' | 'caller_destructure' | 'type_annotation'"""

    confidence: float
    """type_annotation=0.95, test_assertEqual=0.9, caller_destructure=0.7"""


@dataclass(frozen=True)
class RoundtripContract:
    """Roundtrip/serialization contract: encode then decode preserves value.

    Extracted from tests that assert: decode(encode(x)) == x
    or equivalent patterns (dumps/loads, serialize/deserialize).
    """

    encode_symbol: str
    """Name of the encoding function/method."""

    decode_symbol: str
    """Name of the decoding function/method."""

    test_file: str
    """Test file containing the roundtrip assertion."""

    test_line: int
    """Line of the roundtrip assertion."""

    confidence: float
    """Typically 0.95 (direct test evidence)."""
