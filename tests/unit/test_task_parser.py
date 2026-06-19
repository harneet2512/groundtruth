"""Tests for TaskParser."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


from groundtruth.ai.task_parser import TaskParser
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok


def _populate_store(store: SymbolStore) -> None:
    """Add some symbols to the store for cross-referencing."""
    store.insert_symbol(
        name="getUserById",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(user_id: int) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=1000,
    )
    store.insert_symbol(
        name="NotFoundError",
        kind="class",
        language="python",
        file_path="src/utils/errors.py",
        line_number=1,
        end_line=5,
        is_exported=True,
        signature=None,
        params=None,
        return_type=None,
        documentation=None,
        last_indexed_at=1000,
    )


def _mock_haiku_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.usage = MagicMock(input_tokens=50, output_tokens=20)
    return resp


class TestTaskParser:
    async def test_parse_with_ai(self, in_memory_store: SymbolStore) -> None:
        """AI response is parsed correctly."""
        parser = TaskParser(in_memory_store, api_key="test-key")

        with patch.object(parser._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok(('["getUserById", "NotFoundError"]', 70))
            result = await parser.parse("fix getUserById returning null")

        assert isinstance(result, Ok)
        assert "getUserById" in result.value
        assert "NotFoundError" in result.value

    async def test_fallback_no_api_key(self, in_memory_store: SymbolStore) -> None:
        """Without API key, fallback parsing works."""
        parser = TaskParser(in_memory_store, api_key=None)
        result = await parser.parse("fix getUserById returning null instead of NotFoundError")

        assert isinstance(result, Ok)
        assert "getUserById" in result.value
        assert "NotFoundError" in result.value

    async def test_fallback_on_invalid_json(self, in_memory_store: SymbolStore) -> None:
        """Invalid JSON from AI triggers fallback."""
        parser = TaskParser(in_memory_store, api_key="test-key")

        with patch.object(parser._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok(("not valid json {{{", 70))
            result = await parser.parse("fix getUserById")

        assert isinstance(result, Ok)
        assert "getUserById" in result.value

    async def test_stop_word_filtering(self, in_memory_store: SymbolStore) -> None:
        """Stop words are filtered in fallback."""
        parser = TaskParser(in_memory_store, api_key=None)
        result = await parser.parse("fix the getUserById function")

        assert isinstance(result, Ok)
        assert "getUserById" in result.value
        # "fix", "the", "function" are stop words or non-symbols
        for val in result.value:
            assert val.lower() not in {"fix", "the"}

    async def test_matched_symbols_sorted_first(self, in_memory_store: SymbolStore) -> None:
        """Symbols found in the index are sorted before unmatched ones."""
        _populate_store(in_memory_store)
        parser = TaskParser(in_memory_store, api_key="test-key")

        with patch.object(parser._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok(('["unknownFunc", "getUserById"]', 70))
            result = await parser.parse("fix stuff")

        assert isinstance(result, Ok)
        symbols = result.value
        assert symbols[0] == "getUserById"
        assert "unknownFunc" in symbols

    async def test_api_error_graceful_fallback(self, in_memory_store: SymbolStore) -> None:
        """API error falls back gracefully."""
        parser = TaskParser(in_memory_store, api_key="test-key")

        with patch.object(parser._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Err(GroundTruthError(code="ai_api_error", message="Server error"))
            result = await parser.parse("fix getUserById")

        assert isinstance(result, Ok)
        assert "getUserById" in result.value

    async def test_snake_case_extraction(self, in_memory_store: SymbolStore) -> None:
        """snake_case tokens are extracted."""
        parser = TaskParser(in_memory_store, api_key=None)
        result = await parser.parse("update the get_user_by_id function")

        assert isinstance(result, Ok)
        assert "get_user_by_id" in result.value

    async def test_empty_ai_list_fallback(self, in_memory_store: SymbolStore) -> None:
        """Empty list from AI triggers fallback."""
        parser = TaskParser(in_memory_store, api_key="test-key")

        with patch.object(parser._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok(("[]", 30))
            result = await parser.parse("fix getUserById")

        assert isinstance(result, Ok)
        assert "getUserById" in result.value
