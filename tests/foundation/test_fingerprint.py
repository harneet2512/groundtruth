"""Tests for FingerprintExtractor (fingerprint_v1)."""

from __future__ import annotations

import textwrap

import pytest

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.similarity.fingerprint import FingerprintExtractor, FINGERPRINT_SIZE


@pytest.fixture
def extractor() -> FingerprintExtractor:
    return FingerprintExtractor()


def _make_symbol(
    name: str = "test_func",
    kind: str = "method",
    raw_text: str = "",
    parameters: list[str] | None = None,
    parent_class: str | None = None,
) -> ExtractedSymbol:
    return ExtractedSymbol(
        name=name,
        kind=kind,
        language="python",
        start_line=0,
        end_line=10,
        parameters=parameters or [],
        parent_class=parent_class,
        raw_text=raw_text,
    )


class TestFingerprintIdentical:
    """Two identical methods should produce identical fingerprints."""

    def test_identical_methods(self, extractor: FingerprintExtractor) -> None:
        code = textwrap.dedent("""\
            def process(self, x, y):
                if x > 0:
                    return x + y
                return None
        """)
        sym_a = _make_symbol(name="process", raw_text=code, parameters=["x", "y"])
        sym_b = _make_symbol(name="process", raw_text=code, parameters=["x", "y"])

        fp_a = extractor.extract(sym_a)
        fp_b = extractor.extract(sym_b)

        assert fp_a == fp_b
        assert len(fp_a) == FINGERPRINT_SIZE
        assert extractor.distance(fp_a, fp_b) == 0.0


class TestFingerprintSameBodyDifferentName:
    """Same body, different name should produce identical fingerprint."""

    def test_same_body_different_name(self, extractor: FingerprintExtractor) -> None:
        code = textwrap.dedent("""\
            def placeholder(self, items):
                for item in items:
                    if item.valid:
                        self.results.append(item)
                return self.results
        """)
        sym_a = _make_symbol(name="process_items", raw_text=code, parameters=["items"])
        sym_b = _make_symbol(name="handle_entries", raw_text=code, parameters=["items"])

        fp_a = extractor.extract(sym_a)
        fp_b = extractor.extract(sym_b)

        assert fp_a == fp_b


class TestFingerprintDifferentArity:
    """Added parameter should change arity byte, producing different fingerprint."""

    def test_different_arity(self, extractor: FingerprintExtractor) -> None:
        code = textwrap.dedent("""\
            def func():
                return 42
        """)
        sym_a = _make_symbol(name="func", raw_text=code, parameters=[])
        sym_b = _make_symbol(name="func", raw_text=code, parameters=["x", "y"])

        fp_a = extractor.extract(sym_a)
        fp_b = extractor.extract(sym_b)

        assert fp_a != fp_b
        assert extractor.distance(fp_a, fp_b) > 0.0


class TestFingerprintDifferentControlFlow:
    """Different control flow structures should produce different skeleton hash."""

    def test_different_control_flow(self, extractor: FingerprintExtractor) -> None:
        code_if = textwrap.dedent("""\
            def func(x):
                if x > 0:
                    return x
                return None
        """)
        code_loop = textwrap.dedent("""\
            def func(x):
                for i in range(x):
                    try:
                        process(i)
                    except Exception:
                        raise
        """)
        sym_a = _make_symbol(name="func", raw_text=code_if, parameters=["x"])
        sym_b = _make_symbol(name="func", raw_text=code_loop, parameters=["x"])

        fp_a = extractor.extract(sym_a)
        fp_b = extractor.extract(sym_b)

        assert fp_a != fp_b
        assert extractor.distance(fp_a, fp_b) > 0.0


class TestFingerprintProtocol:
    """Verify protocol compliance."""

    def test_rep_type(self, extractor: FingerprintExtractor) -> None:
        assert extractor.rep_type == "fingerprint_v1"

    def test_rep_version(self, extractor: FingerprintExtractor) -> None:
        assert extractor.rep_version == "1.0"

    def test_dimension_is_none(self, extractor: FingerprintExtractor) -> None:
        assert extractor.dimension is None

    def test_invalidation_key(self, extractor: FingerprintExtractor) -> None:
        key_a = extractor.invalidation_key("test.py", "content_a")
        key_b = extractor.invalidation_key("test.py", "content_b")
        key_a2 = extractor.invalidation_key("test.py", "content_a")
        assert key_a != key_b
        assert key_a == key_a2

    def test_empty_body(self, extractor: FingerprintExtractor) -> None:
        sym = _make_symbol(raw_text="")
        fp = extractor.extract(sym)
        assert len(fp) == FINGERPRINT_SIZE

    def test_distance_range(self, extractor: FingerprintExtractor) -> None:
        code_a = "def a(): return 1"
        code_b = textwrap.dedent("""\
            def b(x, y, z):
                for i in range(x):
                    if i > y:
                        raise ValueError()
                return z
        """)
        fp_a = extractor.extract(_make_symbol(raw_text=code_a))
        fp_b = extractor.extract(_make_symbol(raw_text=code_b, parameters=["x", "y", "z"]))
        d = extractor.distance(fp_a, fp_b)
        assert 0.0 <= d <= 1.0
