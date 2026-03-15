"""Parses natural language task descriptions into symbol names."""

from __future__ import annotations

import json
import re

from groundtruth.ai.client import AIClient
from groundtruth.ai.prompts import TASK_PARSER_SYSTEM, TASK_PARSER_USER
from groundtruth.index.store import SymbolStore
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result
from groundtruth.utils.sanitize import sanitize_for_prompt

log = get_logger("ai.task_parser")

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "because",
        "if",
        "when",
        "while",
        "where",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "they",
        "them",
        "their",
        "fix",
        "add",
        "update",
        "change",
        "modify",
        "remove",
        "delete",
        "implement",
        "create",
        "make",
        "use",
        "using",
        "instead",
        "returning",
        "return",
        "null",
        "none",
        "error",
        "bug",
        "issue",
        "problem",
        "throwing",
        "throw",
        "handle",
        "handling",
    }
)


class TaskParser:
    """Extracts likely symbol names from task descriptions."""

    def __init__(self, store: SymbolStore, api_key: str | None = None) -> None:
        self._store = store
        self._api_key = api_key
        self._client = AIClient(api_key)

    async def parse(self, description: str) -> Result[list[str], GroundTruthError]:
        """Parse a task description into symbol names."""
        description = sanitize_for_prompt(description, max_length=2000)
        if not self._client.available:
            return Ok(self._fallback_parse(description))

        prompt_user = TASK_PARSER_USER.format(description=description)
        result = await self._client.complete(TASK_PARSER_SYSTEM, prompt_user, max_tokens=256)

        if isinstance(result, Err):
            log.warning("ai_call_failed", error=result.error.code)
            return Ok(self._fallback_parse(description))

        text, _tokens = result.value
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                return Ok(self._fallback_parse(description))
            symbols = [s for s in parsed if isinstance(s, str) and s.strip()]
            if not symbols:
                return Ok(self._fallback_parse(description))
        except (json.JSONDecodeError, ValueError):
            log.warning("ai_json_parse_failed", response=text[:200])
            return Ok(self._fallback_parse(description))

        return Ok(self._sort_by_index(symbols))

    def _fallback_parse(self, description: str) -> list[str]:
        """Extract symbol-like tokens without AI."""
        tokens = re.split(r"\s+", description)
        symbols: list[str] = []
        seen: set[str] = set()

        for token in tokens:
            clean = re.sub(r"[^\w]", "", token)
            if not clean or clean.lower() in _STOP_WORDS:
                continue

            if self._looks_like_symbol(clean):
                if clean not in seen:
                    symbols.append(clean)
                    seen.add(clean)

        return symbols

    def _looks_like_symbol(self, token: str) -> bool:
        """Check if a token looks like a code symbol."""
        if re.search(r"[a-z][A-Z]", token):
            return True
        if "_" in token:
            return True
        if token[0].isupper() and any(c.islower() for c in token):
            return True
        if token.isupper() and len(token) > 1:
            return True
        return False

    def _sort_by_index(self, symbols: list[str]) -> list[str]:
        """Sort symbols so that those found in the index come first."""
        matched: list[str] = []
        unmatched: list[str] = []
        for sym in symbols:
            result = self._store.find_symbol_by_name(sym)
            if isinstance(result, Ok) and result.value:
                matched.append(sym)
            else:
                unmatched.append(sym)
        return matched + unmatched
