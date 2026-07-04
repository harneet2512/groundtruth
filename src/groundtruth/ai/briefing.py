"""Proactive briefing engine: intent -> FTS5 -> AI -> briefing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from groundtruth.ai.client import AIClient
from groundtruth.ai.prompts import BRIEFING_SYSTEM, BRIEFING_USER
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result
from groundtruth.utils.sanitize import sanitize_for_prompt

log = get_logger("ai.briefing")

_BRIEFING_STOP_WORDS = frozenset(
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
        "and",
        "but",
        "or",
        "not",
        "so",
        "if",
        "when",
        "while",
        "how",
        "what",
        "which",
        "this",
        "that",
        "it",
        "i",
        "we",
        "you",
        "they",
        "add",
        "fix",
        "update",
        "change",
        "make",
        "use",
        "implement",
        "create",
    }
)


@dataclass
class BriefingResult:
    """The output of a briefing request."""

    briefing: str
    relevant_symbols: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BriefingEngine:
    """Generates proactive briefings for developers."""

    def __init__(self, store: SymbolStore, api_key: str | None = None) -> None:
        self._store = store
        self._api_key = api_key
        self._client = AIClient(api_key)

    async def generate_briefing(
        self, intent: str, target_file: str | None = None
    ) -> Result[BriefingResult, GroundTruthError]:
        """Generate a briefing for the given intent."""
        keywords = self._extract_keywords(intent)
        symbols = self._find_symbols(keywords)

        # Enrich with target file symbols
        if target_file is not None:
            file_result = self._store.get_symbols_in_file(target_file)
            if isinstance(file_result, Ok):
                seen_ids = {s.id for s in symbols}
                for sym in file_result.value:
                    if sym.id not in seen_ids:
                        symbols.append(sym)
                        seen_ids.add(sym.id)

        if not symbols:
            return Ok(
                BriefingResult(
                    briefing="No relevant symbols found in the index for this intent.",
                )
            )

        relevant = self._build_relevant_symbols(symbols)

        if not self._client.available:
            return Ok(self._build_no_ai_briefing(symbols, relevant))

        symbols_context = self._format_symbols_context(symbols)
        target_context = ""
        if target_file is not None:
            target_context = f"Target file: {target_file}\n\n"

        prompt_user = BRIEFING_USER.format(
            intent=intent,
            target_file_context=target_context,
            symbols_context=symbols_context,
        )

        result = await self._client.complete(BRIEFING_SYSTEM, prompt_user, max_tokens=512)
        if isinstance(result, Err):
            return Err(result.error)

        text, _tokens = result.value
        warnings = self._extract_warnings(text)
        briefing_text = self._strip_warnings(text)

        return Ok(
            BriefingResult(
                briefing=briefing_text,
                relevant_symbols=relevant,
                warnings=warnings,
            )
        )

    def _extract_keywords(self, intent: str) -> list[str]:
        """Split intent into searchable keywords."""
        words = re.split(r"\W+", intent)
        return [w for w in words if w and w.lower() not in _BRIEFING_STOP_WORDS]

    def _find_symbols(self, keywords: list[str]) -> list[SymbolRecord]:
        """Search for symbols matching keywords via FTS5."""
        if not keywords:
            return []

        query = " OR ".join(keywords)
        result = self._store.search_symbols_fts(query, limit=20)
        if isinstance(result, Err):
            log.warning("fts_search_failed", error=result.error.message)
            return []
        return result.value

    def _build_relevant_symbols(self, symbols: list[SymbolRecord]) -> list[dict[str, str]]:
        """Build the relevant_symbols list, capped at 10."""
        result: list[dict[str, str]] = []
        for sym in symbols[:10]:
            entry: dict[str, str] = {
                "name": sym.name,
                "file": sym.file_path,
            }
            if sym.signature:
                entry["signature"] = sym.signature
            result.append(entry)
        return result

    def _format_symbols_context(self, symbols: list[SymbolRecord]) -> str:
        """Format symbols for the AI prompt (with sanitization)."""
        lines: list[str] = []
        for sym in symbols:
            name = sanitize_for_prompt(sym.name, max_length=200)
            parts = [f"- {name} ({sym.kind}) in {sym.file_path}"]
            if sym.signature:
                parts.append(f"  Signature: {sanitize_for_prompt(sym.signature, max_length=500)}")
            if sym.documentation:
                parts.append(f"  Docs: {sanitize_for_prompt(sym.documentation, max_length=500)}")
            lines.append("\n".join(parts))
        return "\n".join(lines)

    def _build_no_ai_briefing(
        self,
        symbols: list[SymbolRecord],
        relevant: list[dict[str, str]],
    ) -> BriefingResult:
        """Build a briefing from raw data without AI."""
        lines: list[str] = []
        for sym in symbols[:10]:
            line = f"- {sym.name} ({sym.kind}) in {sym.file_path}"
            if sym.signature:
                line += f" — {sym.signature}"
            lines.append(line)
        return BriefingResult(
            briefing="Relevant symbols:\n" + "\n".join(lines),
            relevant_symbols=relevant,
        )

    def _extract_warnings(self, text: str) -> list[str]:
        """Extract WARNING: lines from AI response."""
        warnings: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.upper().startswith("WARNING:"):
                warnings.append(stripped[len("WARNING:") :].strip())
        return warnings

    def _strip_warnings(self, text: str) -> str:
        """Remove WARNING: lines from the briefing text."""
        lines = [
            line for line in text.split("\n") if not line.strip().upper().startswith("WARNING:")
        ]
        return "\n".join(lines).strip()
