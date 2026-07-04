"""Tests for v2.2 test_link module."""
from __future__ import annotations

from groundtruth.pretask.test_link import (
    candidate_source_stems,
    extract_test_names_from_query,
    score_test_to_source,
)
from groundtruth.pretask.v2_types import HighSignalToken, QueryObject


def test_extract_test_names_from_function_hints() -> None:
    q = QueryObject(function_hints=["test_foo", "regular_func"])
    assert extract_test_names_from_query(q) == {"test_foo"}


def test_extract_test_names_from_tokens() -> None:
    q = QueryObject(
        high_signal_tokens=[
            HighSignalToken(token="test_foo_bar", weight=1.0, source="backtick"),
            HighSignalToken(token="other", weight=1.0, source="backtick"),
        ]
    )
    assert "test_foo_bar" in extract_test_names_from_query(q)


def test_extract_test_names_from_file_hints() -> None:
    q = QueryObject(file_hints=["tests/test_auth.py"])
    assert "test_auth" in extract_test_names_from_query(q)


def test_candidate_stems_simple() -> None:
    stems = candidate_source_stems("test_foo")
    assert "foo" in stems


def test_candidate_stems_compound() -> None:
    stems = candidate_source_stems("test_foo_bar")
    assert "foo" in stems
    assert "foo_bar" in stems


def test_candidate_stems_camelcase() -> None:
    stems = candidate_source_stems("testFooBar")
    assert "foo_bar" in stems or "foobar" in stems or "foo" in stems


def test_score_basic() -> None:
    q = QueryObject(function_hints=["test_auth"])
    scores = score_test_to_source(["src/auth.py", "src/users.py"], q)
    assert scores.get("src/auth.py") == 1.0
    assert scores.get("src/users.py", 0.0) == 0.0


def test_score_excludes_test_files() -> None:
    q = QueryObject(function_hints=["test_auth"])
    scores = score_test_to_source(["src/auth.py", "tests/test_auth.py"], q)
    assert scores.get("src/auth.py") == 1.0
    assert "tests/test_auth.py" not in scores


def test_score_substring_match() -> None:
    q = QueryObject(function_hints=["test_helpers"])
    scores = score_test_to_source(["src/auth_helpers.py"], q)
    # only one positive file -> normalized to 1.0; raw was 0.7
    assert "src/auth_helpers.py" in scores
    assert scores["src/auth_helpers.py"] == 1.0


def test_score_substring_match_relative() -> None:
    q = QueryObject(function_hints=["test_helpers"])
    scores = score_test_to_source(
        ["src/helpers.py", "src/auth_helpers.py"], q
    )
    # exact wins (1.0), substring (0.7) normalized to 0.7
    assert scores["src/helpers.py"] == 1.0
    assert scores["src/auth_helpers.py"] < 1.0
    assert scores["src/auth_helpers.py"] > 0.0


def test_score_segment_match() -> None:
    q = QueryObject(function_hints=["test_login"])
    scores = score_test_to_source(
        ["src/auth/login_handler.py", "src/other/foo.py"], q
    )
    assert "src/auth/login_handler.py" in scores
    assert scores["src/auth/login_handler.py"] > 0.0


def test_score_no_test_names_returns_empty() -> None:
    q = QueryObject(function_hints=["regular_func", "another_one"])
    scores = score_test_to_source(["src/auth.py", "src/users.py"], q)
    assert scores == {}


def test_score_normalized_to_max_1() -> None:
    q = QueryObject(function_hints=["test_auth"])
    scores = score_test_to_source(
        ["src/auth.py", "src/auth_handlers.py", "src/users.py"], q
    )
    assert any(v == 1.0 for v in scores.values())


def test_score_handles_camelcase_tests() -> None:
    q = QueryObject(function_hints=["testRidgeClassifier"])
    scores = score_test_to_source(
        ["sklearn/linear_model/ridge.py", "sklearn/svm/base.py"], q
    )
    assert "sklearn/linear_model/ridge.py" in scores
    assert scores["sklearn/linear_model/ridge.py"] > 0.0


def test_all_test_files_returns_empty() -> None:
    q = QueryObject(function_hints=["test_auth"])
    scores = score_test_to_source(
        ["tests/test_auth.py", "tests/test_users.py"], q
    )
    assert scores == {}
