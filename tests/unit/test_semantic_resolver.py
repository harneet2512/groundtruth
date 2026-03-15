"""Tests for SemanticResolver."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


from groundtruth.ai.semantic_resolver import SemanticResolver
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok


def _populate_store(store: SymbolStore) -> None:
    """Add symbols for resolver tests."""
    store.insert_symbol(
        name="hashPassword",
        kind="function",
        language="python",
        file_path="src/utils/crypto.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(password: str) -> str",
        params=None,
        return_type="str",
        documentation=None,
        last_indexed_at=1000,
    )
    store.insert_symbol(
        name="verifyPassword",
        kind="function",
        language="python",
        file_path="src/utils/crypto.py",
        line_number=12,
        end_line=20,
        is_exported=True,
        signature="(password: str, hash: str) -> bool",
        params=None,
        return_type="bool",
        documentation=None,
        last_indexed_at=1000,
    )


class TestSemanticResolver:
    async def test_resolve_with_ai(self, in_memory_store: SymbolStore) -> None:
        """AI resolution returns correct result."""
        _populate_store(in_memory_store)
        resolver = SemanticResolver(in_memory_store, api_key="test-key")

        ai_response = (
            '{"intended_symbol": "hashPassword", '
            '"suggested_fix": "from src.utils.crypto import hashPassword", '
            '"confidence": 0.95, '
            '"reasoning": "The developer meant hashPassword from crypto module"}'
        )

        with patch.object(resolver._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok((ai_response, 100))
            result = await resolver.resolve(
                "hashPasswrd not found",
                "from auth import hashPasswrd",
                "src/app.py",
            )

        assert isinstance(result, Ok)
        assert result.value.intended_symbol == "hashPassword"
        assert result.value.confidence == 0.95

    async def test_no_api_key(self, in_memory_store: SymbolStore) -> None:
        """No API key returns error."""
        resolver = SemanticResolver(in_memory_store, api_key=None)
        result = await resolver.resolve("error", "code", "file.py")

        assert isinstance(result, Err)
        assert result.error.code == "no_api_key"

    async def test_invalid_json(self, in_memory_store: SymbolStore) -> None:
        """Invalid JSON from AI returns parse error."""
        resolver = SemanticResolver(in_memory_store, api_key="test-key")

        with patch.object(resolver._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok(("not json at all", 50))
            result = await resolver.resolve("error", "code", "file.py")

        assert isinstance(result, Err)
        assert result.error.code == "ai_parse_error"

    async def test_confidence_clamping(self, in_memory_store: SymbolStore) -> None:
        """Confidence > 1.0 is clamped to 1.0."""
        resolver = SemanticResolver(in_memory_store, api_key="test-key")

        ai_response = (
            '{"intended_symbol": "foo", "suggested_fix": "bar", '
            '"confidence": 5.0, "reasoning": "sure"}'
        )

        with patch.object(resolver._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok((ai_response, 50))
            result = await resolver.resolve("error", "code", "file.py")

        assert isinstance(result, Ok)
        assert result.value.confidence == 1.0

    async def test_related_symbols_search(self, in_memory_store: SymbolStore) -> None:
        """Related symbols are found and included in context."""
        _populate_store(in_memory_store)
        resolver = SemanticResolver(in_memory_store, api_key="test-key")

        ai_response = (
            '{"intended_symbol": "hashPassword", "suggested_fix": "fix", '
            '"confidence": 0.9, "reasoning": "found it"}'
        )

        with patch.object(resolver._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok((ai_response, 50))
            result = await resolver.resolve(
                "hashPassword not found in auth",
                "from auth import hashPassword",
                "src/app.py",
            )

        assert isinstance(result, Ok)
        # Verify complete was called with symbols context containing hashPassword
        call_args = mock.call_args
        user_prompt = call_args[1].get("user") or call_args[0][1]
        assert "hashPassword" in user_prompt

    async def test_api_error_propagation(self, in_memory_store: SymbolStore) -> None:
        """API error is propagated."""
        resolver = SemanticResolver(in_memory_store, api_key="test-key")

        with patch.object(resolver._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Err(GroundTruthError(code="ai_api_error", message="Server error"))
            result = await resolver.resolve("error", "code", "file.py")

        assert isinstance(result, Err)
        assert result.error.code == "ai_api_error"
