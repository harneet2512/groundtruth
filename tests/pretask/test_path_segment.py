"""Unit tests for v2.2 path-segment BM25 scoring."""
from __future__ import annotations

from groundtruth.pretask.path_segment import (
    _tokenize_path,
    score_from_query,
    score_path_segment_match,
)
from groundtruth.pretask.v2_types import HighSignalToken, QueryObject


def test_tokenize_path_basic() -> None:
    toks = set(_tokenize_path("sklearn/linear_model/ridge.py"))
    for expected in ("sklearn", "linear", "model", "ridge", "linear_model"):
        assert expected in toks, f"missing {expected!r} in {toks}"


def test_tokenize_path_strips_extension() -> None:
    toks = set(_tokenize_path("sklearn/linear_model/ridge.py"))
    assert "py" not in toks
    js_toks = set(_tokenize_path("src/foo/bar.js"))
    assert "js" not in js_toks


def test_score_empty_query_returns_zeros() -> None:
    files = ["a/b.py", "c/d.py"]
    out = score_path_segment_match(files, [])
    assert out == {"a/b.py": 0.0, "c/d.py": 0.0}


def test_score_exact_segment_match() -> None:
    files = ["sklearn/linear_model/ridge.py", "sklearn/kernel_ridge.py"]
    out = score_path_segment_match(files, [("linear_model", 4.0)])
    assert out["sklearn/linear_model/ridge.py"] > out["sklearn/kernel_ridge.py"]


def test_score_normalized_to_max_1() -> None:
    files = ["sklearn/linear_model/ridge.py", "sklearn/kernel_ridge.py", "numpy/core/array.py"]
    out = score_path_segment_match(files, [("linear_model", 4.0), ("ridge", 2.0)])
    assert max(out.values()) == 1.0


def test_score_lookalike_file_loses() -> None:
    files = [
        "sklearn/linear_model/X.py",
        "sklearn/kernel_ridge.py",
    ]
    out = score_path_segment_match(files, [("linear_model", 4.0)])
    assert out["sklearn/linear_model/X.py"] > out["sklearn/kernel_ridge.py"]
    assert out["sklearn/kernel_ridge.py"] == 0.0


def test_score_from_query_uses_all_hint_types() -> None:
    files = [
        "src/foo.py",
        "src/bar.py",
        "src/other/baz.py",
    ]
    query = QueryObject(
        file_hints=["foo.py"],
        function_hints=["bar"],
    )
    out = score_from_query(files, query)
    assert out["src/foo.py"] > 0.0
    assert out["src/bar.py"] > 0.0
    assert out["src/other/baz.py"] == 0.0


def test_score_handles_dotted_qualifier() -> None:
    files = [
        "sklearn/linear_model/ridge.py",
        "sklearn/linear_model/_base.py",
        "sklearn/kernel_ridge.py",
        "numpy/core/array.py",
    ]
    query = QueryObject(
        high_signal_tokens=[
            HighSignalToken(token="RidgeClassifierCV", weight=4.0, source="backtick"),
            HighSignalToken(token="linear_model", weight=4.0, source="backtick"),
        ],
    )
    out = score_from_query(files, query)
    ridge_lm = out["sklearn/linear_model/ridge.py"]
    base_lm = out["sklearn/linear_model/_base.py"]
    kernel = out["sklearn/kernel_ridge.py"]
    array = out["numpy/core/array.py"]
    assert ridge_lm > kernel
    assert base_lm > kernel
    assert ridge_lm > array
    assert array == 0.0
