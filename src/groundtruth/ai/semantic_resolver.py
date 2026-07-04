"""AI semantic resolver -- fallback when deterministic methods fail."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from groundtruth.ai.client import AIClient
from groundtruth.ai.prompts import SEMANTIC_RESOLVER_SYSTEM, SEMANTIC_RESOLVER_USER
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result
from groundtruth.utils.sanitize import sanitize_for_prompt

log = get_logger("ai.semantic_resolver")


@dataclass
class ResolutionResult:
    """Result of semantic resolution."""

    intended_symbol: str
    suggested_fix: str
    confidence: float
    reasoning: str


class SemanticResolver:
    """Resolves ambiguous symbols using AI when deterministic methods fail."""

    def __init__(self, store: SymbolStore, api_key: str | None = None) -> None:
        self._store = store
        self._api_key = api_key
        self._client = AIClient(api_key)

    async def resolve(
        self,
        error_message: str,
        code_context: str,
        file_path: str,
    ) -> Result[ResolutionResult, GroundTruthError]:
        """Resolve an ambiguous symbol using AI."""
        if not self._client.available:
            return Err(
                GroundTruthError(
                    code="no_api_key",
                    message="No API key configured for AI resolution",
                )
            )

        related = self._find_related_symbols(error_message)
        symbols_context = self._format_related_symbols(related)

        prompt_user = SEMANTIC_RESOLVER_USER.format(
            error_message=error_message,
            code_context=code_context,
            file_path=file_path,
            symbols_context=symbols_context,
        )

        result = await self._client.complete(SEMANTIC_RESOLVER_SYSTEM, prompt_user, max_tokens=512)
        if isinstance(result, Err):
            return Err(result.error)

        text, _tokens = result.value
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                return Err(
                    GroundTruthError(
                        code="ai_parse_error",
                        message="AI response was not a JSON object",
                    )
                )

            confidence = float(parsed.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))

            return Ok(
                ResolutionResult(
                    intended_symbol=str(parsed.get("intended_symbol", "")),
                    suggested_fix=str(parsed.get("suggested_fix", "")),
                    confidence=confidence,
                    reasoning=str(parsed.get("reasoning", "")),
                )
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return Err(
                GroundTruthError(
                    code="ai_parse_error",
                    message=f"Failed to parse AI response: {exc}",
                )
            )

    def _find_related_symbols(self, error_message: str) -> list[SymbolRecord]:
        """Extract symbol-like words from error and search the index."""
        words = re.split(r"\W+", error_message)
        symbol_words = [
            w
            for w in words
            if w
            and (
                re.search(r"[a-z][A-Z]", w)
                or "_" in w
                or (w[0].isupper() and any(c.islower() for c in w))
            )
        ]

        seen_ids: set[int] = set()
        results: list[SymbolRecord] = []
        for word in symbol_words:
            fts_result = self._store.search_symbols_fts(word, limit=5)
            if isinstance(fts_result, Ok):
                for sym in fts_result.value:
                    if sym.id not in seen_ids:
                        results.append(sym)
                        seen_ids.add(sym.id)
            if len(results) >= 20:
                break

        return results[:20]

    def _format_related_symbols(self, symbols: list[SymbolRecord]) -> str:
        """Format related symbols for the AI prompt (with sanitization)."""
        if not symbols:
            return "(none found)"
        lines: list[str] = []
        for sym in symbols:
            name = sanitize_for_prompt(sym.name, max_length=200)
            line = f"- {name} ({sym.kind}) in {sym.file_path}"
            if sym.signature:
                line += f" — {sanitize_for_prompt(sym.signature, max_length=500)}"
            lines.append(line)
        return "\n".join(lines)
