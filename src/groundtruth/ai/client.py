"""Shared async AI client wrapping the Anthropic SDK."""

from __future__ import annotations

from typing import Any

from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

MODEL = "claude-haiku-4-5-20251001"

log = get_logger("ai.client")


class AIClient:
    """Thin async wrapper around anthropic.AsyncAnthropic."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client: Any = None

    @property
    def available(self) -> bool:
        """Whether an API key is configured."""
        return self._api_key is not None

    def _get_client(self) -> Any:
        """Lazily instantiate the Anthropic client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
    ) -> Result[tuple[str, int], GroundTruthError]:
        """Call Claude and return (response_text, tokens_used)."""
        try:
            import anthropic
        except ImportError:
            return Err(
                GroundTruthError(
                    code="ai_import_error",
                    message="anthropic package is not installed",
                )
            )

        try:
            client = self._get_client()
            response = await client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = response.content[0].text
            tokens = response.usage.input_tokens + response.usage.output_tokens
            return Ok((text, tokens))
        except anthropic.AuthenticationError:
            return Err(
                GroundTruthError(
                    code="ai_auth_error",
                    message="Invalid API key",
                )
            )
        except anthropic.RateLimitError:
            return Err(
                GroundTruthError(
                    code="ai_rate_limit",
                    message="Rate limit exceeded",
                )
            )
        except anthropic.APIError as exc:
            return Err(
                GroundTruthError(
                    code="ai_api_error",
                    message=f"API error: {exc}",
                )
            )
