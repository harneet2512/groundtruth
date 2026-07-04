"""Tests for AIClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from groundtruth.ai.client import AIClient
from groundtruth.utils.result import Err, Ok

try:
    import anthropic  # noqa: F401

    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

pytestmark = pytest.mark.skipif(not HAS_ANTHROPIC, reason="anthropic not installed")


class TestAIClient:
    def test_available_with_key(self) -> None:
        client = AIClient(api_key="test-key")
        assert client.available is True

    def test_not_available_without_key(self) -> None:
        client = AIClient(api_key=None)
        assert client.available is False

    async def test_complete_returns_ok(self) -> None:
        client = AIClient(api_key="test-key")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="hello")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        mock_inner = MagicMock()
        mock_inner.messages.create = AsyncMock(return_value=mock_response)
        client._client = mock_inner

        result = await client.complete("system", "user")

        assert isinstance(result, Ok)
        text, tokens = result.value
        assert text == "hello"
        assert tokens == 15

    async def test_auth_error(self) -> None:
        client = AIClient(api_key="bad-key")
        mock_inner = MagicMock()
        mock_inner.messages.create = AsyncMock(
            side_effect=anthropic.AuthenticationError(
                message="bad key",
                response=MagicMock(status_code=401),
                body=None,
            )
        )
        client._client = mock_inner

        result = await client.complete("system", "user")

        assert isinstance(result, Err)
        assert result.error.code == "ai_auth_error"

    async def test_rate_limit_error(self) -> None:
        client = AIClient(api_key="test-key")
        mock_inner = MagicMock()
        mock_inner.messages.create = AsyncMock(
            side_effect=anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body=None,
            )
        )
        client._client = mock_inner

        result = await client.complete("system", "user")

        assert isinstance(result, Err)
        assert result.error.code == "ai_rate_limit"
