"""Tests for symbol component splitting and matching."""

from __future__ import annotations

from groundtruth.utils.symbol_components import split_symbol_name, suggest_by_components


class TestSplitSymbolName:
    def test_snake_case(self) -> None:
        assert split_symbol_name("get_user_by_id") == ["get", "user", "by", "id"]

    def test_camel_case(self) -> None:
        assert split_symbol_name("getUserById") == ["get", "user", "by", "id"]

    def test_pascal_case(self) -> None:
        assert split_symbol_name("GetUserById") == ["get", "user", "by", "id"]

    def test_screaming_snake(self) -> None:
        assert split_symbol_name("MAX_RETRY_COUNT") == ["max", "retry", "count"]

    def test_acronym_handling(self) -> None:
        result = split_symbol_name("HTTPClient")
        assert result == ["http", "client"]

    def test_xml_parser(self) -> None:
        result = split_symbol_name("XMLParser")
        assert result == ["xml", "parser"]

    def test_empty_string(self) -> None:
        assert split_symbol_name("") == []

    def test_single_word(self) -> None:
        assert split_symbol_name("connect") == ["connect"]

    def test_mixed_snake_camel(self) -> None:
        # e.g. "get_UserById" → ["get", "user", "by", "id"]
        result = split_symbol_name("get_UserById")
        assert result == ["get", "user", "by", "id"]


class TestSuggestByComponents:
    def test_finds_shared_components(self) -> None:
        candidates = ["getUserById", "getUserByEmail", "deleteUser", "formatDate"]
        results = suggest_by_components("getUser", candidates)
        # getUserById and getUserByEmail share "get" and "user"
        names = [r[0] for r in results]
        assert "getUserById" in names
        assert "getUserByEmail" in names

    def test_min_overlap_filter(self) -> None:
        candidates = ["getUserById", "formatDate"]
        results = suggest_by_components("getUser", candidates, min_overlap=2)
        names = [r[0] for r in results]
        assert "getUserById" in names
        assert "formatDate" not in names

    def test_score_calculation(self) -> None:
        candidates = ["getUserById"]
        results = suggest_by_components("getUserById", candidates)
        assert len(results) == 1
        assert results[0][1] == 1.0  # perfect match

    def test_max_results(self) -> None:
        candidates = [f"func_{i}" for i in range(10)]
        results = suggest_by_components("func_0", candidates, max_results=3)
        assert len(results) <= 3

    def test_empty_name(self) -> None:
        assert suggest_by_components("", ["foo", "bar"]) == []

    def test_no_matches(self) -> None:
        results = suggest_by_components("alpha", ["beta", "gamma"], min_overlap=1)
        assert results == []

    def test_sorted_by_score_then_levenshtein(self) -> None:
        candidates = ["getUserById", "getUser"]
        results = suggest_by_components("getUser", candidates)
        # getUser is a closer match
        assert len(results) >= 1
        # First result should have highest score
        if len(results) >= 2:
            assert results[0][1] >= results[1][1]
