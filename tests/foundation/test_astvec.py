"""Tests for StructuralVectorExtractor (astvec_v1)."""

from __future__ import annotations

import math
import struct
import textwrap

import pytest

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.similarity.astvec import (
    VECTOR_DIM,
    StructuralVectorExtractor,
    extract_astvec_features,
)


@pytest.fixture
def extractor() -> StructuralVectorExtractor:
    return StructuralVectorExtractor()


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


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


class TestKnownFeatureValues:
    """Known structure should produce verifiable feature values."""

    def test_return_and_if_detected(self) -> None:
        code = textwrap.dedent("""\
            def func(x):
                if x > 0:
                    return x
                return None
        """)
        features = extract_astvec_features(code, param_count=1)
        assert len(features) == VECTOR_DIM

        # Bucket 1: has_return=1 (idx 0), has_raise=0 (idx 2)
        assert features[0] == 1.0  # has_return
        assert features[2] == 0.0  # has_raise

        # Bucket 2: has_if=1 (idx 8)
        assert features[8] == 1.0  # has_if

        # Bucket 4: num_params/10 = 0.1 (idx 20)
        assert features[20] == pytest.approx(0.1)

    def test_raise_and_try(self) -> None:
        code = textwrap.dedent("""\
            def func():
                try:
                    do_something()
                except Exception:
                    raise ValueError("oops")
        """)
        features = extract_astvec_features(code, param_count=0)

        assert features[2] == 1.0   # has_raise
        assert features[11] == 1.0  # has_try_except

    def test_for_loop_with_assignment(self) -> None:
        code = textwrap.dedent("""\
            def func(items):
                result = []
                for item in items:
                    result.append(item)
                return result
        """)
        features = extract_astvec_features(code, param_count=1)

        assert features[0] == 1.0  # has_return
        assert features[3] == 1.0  # has_assignment
        assert features[9] == 1.0  # has_for


class TestSimilarMethods:
    """Two similar methods should have cosine > 0.85."""

    def test_similar_structure(self, extractor: StructuralVectorExtractor) -> None:
        code_a = textwrap.dedent("""\
            def process_items(self, items):
                result = []
                for item in items:
                    if item.valid:
                        result.append(item.value)
                return result
        """)
        code_b = textwrap.dedent("""\
            def filter_entries(self, entries):
                output = []
                for entry in entries:
                    if entry.active:
                        output.append(entry.data)
                return output
        """)
        blob_a = extractor.extract(_make_symbol(raw_text=code_a, parameters=["items"]))
        blob_b = extractor.extract(_make_symbol(raw_text=code_b, parameters=["entries"]))

        dist = extractor.distance(blob_a, blob_b)
        similarity = 1.0 - dist
        assert similarity > 0.85


class TestDifferentMethods:
    """Two structurally different methods should have cosine < 0.5."""

    def test_very_different_structure(self, extractor: StructuralVectorExtractor) -> None:
        code_a = textwrap.dedent("""\
            def simple_getter(self):
                return self.value
        """)
        code_b = textwrap.dedent("""\
            def complex_processor(self, data, config, retries):
                result = {}
                for key in data:
                    try:
                        while retries > 0:
                            val = self.fetch(key)
                            if val is not None:
                                result[key] = val
                                break
                            retries -= 1
                    except Exception as e:
                        raise RuntimeError(f"Failed: {e}")
                assert len(result) > 0
                return result
        """)
        blob_a = extractor.extract(_make_symbol(raw_text=code_a, parameters=[]))
        blob_b = extractor.extract(
            _make_symbol(raw_text=code_b, parameters=["data", "config", "retries"])
        )

        dist = extractor.distance(blob_a, blob_b)
        similarity = 1.0 - dist
        assert similarity < 0.5


class TestEmptyMethod:
    """Empty method should produce a valid zero vector."""

    def test_empty_body(self, extractor: StructuralVectorExtractor) -> None:
        sym = _make_symbol(raw_text="")
        blob = extractor.extract(sym)

        assert len(blob) == VECTOR_DIM * 4  # 128 bytes
        features = list(struct.unpack(f"{VECTOR_DIM}f", blob))
        assert all(v == 0.0 for v in features)

    def test_pass_only(self, extractor: StructuralVectorExtractor) -> None:
        code = "def noop():\n    pass\n"
        sym = _make_symbol(raw_text=code)
        blob = extractor.extract(sym)

        features = list(struct.unpack(f"{VECTOR_DIM}f", blob))
        # has_pass should be 1.0 (index 7)
        assert features[7] == 1.0


class TestProtocol:
    """Verify protocol compliance."""

    def test_rep_type(self, extractor: StructuralVectorExtractor) -> None:
        assert extractor.rep_type == "astvec_v1"

    def test_dimension(self, extractor: StructuralVectorExtractor) -> None:
        assert extractor.dimension == VECTOR_DIM

    def test_distance_range(self, extractor: StructuralVectorExtractor) -> None:
        code_a = "def a(): return 1"
        code_b = "def b():\n    for i in range(10):\n        pass\n"
        blob_a = extractor.extract(_make_symbol(raw_text=code_a))
        blob_b = extractor.extract(_make_symbol(raw_text=code_b))
        d = extractor.distance(blob_a, blob_b)
        assert 0.0 <= d <= 1.0
