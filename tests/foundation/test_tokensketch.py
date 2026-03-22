"""Tests for TokenSketchExtractor (tokensketch_v1)."""

from __future__ import annotations

import textwrap

import pytest

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.similarity.tokensketch import (
    K,
    SKETCH_BYTES,
    TokenSketchExtractor,
    _extract_tokens_ast,
)


@pytest.fixture
def extractor() -> TokenSketchExtractor:
    return TokenSketchExtractor()


def _make_symbol(
    name: str = "test_func",
    kind: str = "function",
    raw_text: str = "",
    parameters: list[str] | None = None,
) -> ExtractedSymbol:
    return ExtractedSymbol(
        name=name,
        kind=kind,
        language="python",
        start_line=0,
        end_line=10,
        parameters=parameters or [],
        raw_text=raw_text,
    )


class TestSameIdentifiers:
    """Same identifiers should produce high Jaccard similarity."""

    def test_identical_code(self, extractor: TokenSketchExtractor) -> None:
        code = textwrap.dedent("""\
            def process(self, items):
                result = self.filter(items)
                return self.transform(result)
        """)
        sym_a = _make_symbol(raw_text=code)
        sym_b = _make_symbol(raw_text=code)

        blob_a = extractor.extract(sym_a)
        blob_b = extractor.extract(sym_b)

        dist = extractor.distance(blob_a, blob_b)
        assert dist == 0.0  # identical code = identical sketch

    def test_same_identifiers_different_order(self, extractor: TokenSketchExtractor) -> None:
        code_a = textwrap.dedent("""\
            def func():
                x = compute_value()
                y = transform_data(x)
                return validate_result(y)
        """)
        code_b = textwrap.dedent("""\
            def func():
                y = transform_data(None)
                x = compute_value()
                result = validate_result(x)
                return result
        """)
        blob_a = extractor.extract(_make_symbol(raw_text=code_a))
        blob_b = extractor.extract(_make_symbol(raw_text=code_b))

        similarity = 1.0 - extractor.distance(blob_a, blob_b)
        # Same identifiers used, should have high similarity
        assert similarity > 0.7


class TestSameStructureDifferentIdentifiers:
    """Same structure but different identifiers should have low Jaccard."""

    def test_different_tokens(self, extractor: TokenSketchExtractor) -> None:
        code_a = textwrap.dedent("""\
            def alpha():
                beaver = compute_cedar()
                delta = transform_eagle(beaver)
                return validate_fox(delta)
        """)
        code_b = textwrap.dedent("""\
            def gamma():
                hawk = fetch_igloo()
                jaguar = process_kite(hawk)
                return check_lemon(jaguar)
        """)
        blob_a = extractor.extract(_make_symbol(raw_text=code_a))
        blob_b = extractor.extract(_make_symbol(raw_text=code_b))

        similarity = 1.0 - extractor.distance(blob_a, blob_b)
        # Very different identifiers, low similarity
        assert similarity < 0.4


class TestCommonTokensDontDominate:
    """Common tokens (self, return, etc.) shouldn't make everything similar."""

    def test_common_tokens_dont_inflate(self, extractor: TokenSketchExtractor) -> None:
        code_a = textwrap.dedent("""\
            def method_a(self):
                self.database_connection.execute("SELECT * FROM users")
                return self.format_user_results()
        """)
        code_b = textwrap.dedent("""\
            def method_b(self):
                self.cache_manager.invalidate("session_keys")
                return self.rebuild_auth_tokens()
        """)
        blob_a = extractor.extract(_make_symbol(raw_text=code_a))
        blob_b = extractor.extract(_make_symbol(raw_text=code_b))

        similarity = 1.0 - extractor.distance(blob_a, blob_b)
        # They share self/return but domain tokens differ
        assert similarity < 0.6


class TestTokenExtraction:
    """Test the tokenization function directly."""

    def test_extracts_identifiers(self) -> None:
        code = "x = compute(y)"
        tokens = _extract_tokens_ast(code)
        assert "x" in tokens
        assert "y" in tokens
        assert "call:compute" in tokens

    def test_extracts_string_keys(self) -> None:
        code = 'data = {"key": value}'
        tokens = _extract_tokens_ast(code)
        assert "str:key" in tokens

    def test_empty_code(self) -> None:
        tokens = _extract_tokens_ast("")
        assert tokens == set()

    def test_normalizes_underscores(self) -> None:
        code = "__private = _value"
        tokens = _extract_tokens_ast(code)
        assert "private" in tokens
        assert "value" in tokens


class TestProtocol:
    """Verify protocol compliance."""

    def test_rep_type(self, extractor: TokenSketchExtractor) -> None:
        assert extractor.rep_type == "tokensketch_v1"

    def test_dimension_is_none(self, extractor: TokenSketchExtractor) -> None:
        assert extractor.dimension is None

    def test_blob_size(self, extractor: TokenSketchExtractor) -> None:
        code = "def f(): return 1"
        blob = extractor.extract(_make_symbol(raw_text=code))
        assert len(blob) == SKETCH_BYTES

    def test_distance_range(self, extractor: TokenSketchExtractor) -> None:
        code_a = "def a(): return compute_alpha()"
        code_b = "def b(): return process_beta()"
        blob_a = extractor.extract(_make_symbol(raw_text=code_a))
        blob_b = extractor.extract(_make_symbol(raw_text=code_b))
        d = extractor.distance(blob_a, blob_b)
        assert 0.0 <= d <= 1.0
