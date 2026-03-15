"""Tests for BriefingEngine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


from groundtruth.ai.briefing import BriefingEngine
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


def _populate_store(store: SymbolStore) -> None:
    """Add symbols for briefing tests."""
    store.insert_symbol(
        name="authMiddleware", kind="function", language="python",
        file_path="src/middleware/auth.py", line_number=1, end_line=20,
        is_exported=True, signature="(request, next) -> Response", params=None,
        return_type="Response", documentation="Auth middleware for routes",
        last_indexed_at=1000,
    )
    store.insert_symbol(
        name="signToken", kind="function", language="python",
        file_path="src/auth/jwt.py", line_number=1, end_line=10,
        is_exported=True, signature="(payload: dict) -> str", params=None,
        return_type="str", documentation="Sign a JWT token",
        last_indexed_at=1000,
    )
    store.insert_symbol(
        name="decodeToken", kind="function", language="python",
        file_path="src/auth/jwt.py", line_number=12, end_line=20,
        is_exported=True, signature="(token: str) -> TokenPayload", params=None,
        return_type="TokenPayload", documentation="Decode a JWT token",
        last_indexed_at=1000,
    )


class TestBriefingEngine:
    async def test_briefing_with_ai(self, in_memory_store: SymbolStore) -> None:
        """AI briefing produces populated result."""
        _populate_store(in_memory_store)
        engine = BriefingEngine(in_memory_store, api_key="test-key")

        ai_response = (
            "Auth is handled via authMiddleware in src/middleware/auth.py. "
            "JWT operations: signToken and decodeToken in src/auth/jwt.py."
        )

        with patch.object(engine._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok((ai_response, 100))
            result = await engine.generate_briefing("add JWT auth middleware")

        assert isinstance(result, Ok)
        br = result.value
        assert "authMiddleware" in br.briefing
        assert len(br.relevant_symbols) > 0

    async def test_no_api_key_raw_briefing(self, in_memory_store: SymbolStore) -> None:
        """Without API key, raw symbol data is returned."""
        _populate_store(in_memory_store)
        engine = BriefingEngine(in_memory_store, api_key=None)

        result = await engine.generate_briefing("add JWT auth")

        assert isinstance(result, Ok)
        br = result.value
        assert "authMiddleware" in br.briefing or "signToken" in br.briefing
        assert len(br.relevant_symbols) > 0

    async def test_no_symbols_found(self, in_memory_store: SymbolStore) -> None:
        """No matching symbols returns appropriate message."""
        engine = BriefingEngine(in_memory_store, api_key="test-key")

        result = await engine.generate_briefing("do something completely unrelated xyz123")

        assert isinstance(result, Ok)
        assert "no relevant symbols" in result.value.briefing.lower()

    async def test_target_file_enrichment(self, in_memory_store: SymbolStore) -> None:
        """Target file symbols are included even without FTS match."""
        _populate_store(in_memory_store)
        engine = BriefingEngine(in_memory_store, api_key=None)

        result = await engine.generate_briefing(
            "do something", target_file="src/auth/jwt.py"
        )

        assert isinstance(result, Ok)
        names = [s["name"] for s in result.value.relevant_symbols]
        assert "signToken" in names or "decodeToken" in names

    async def test_keywords_extraction(self, in_memory_store: SymbolStore) -> None:
        """Keywords are extracted correctly."""
        engine = BriefingEngine(in_memory_store, api_key=None)
        keywords = engine._extract_keywords("add JWT auth middleware to user routes")
        # Stop words removed
        assert "JWT" in keywords
        assert "auth" in keywords
        assert "middleware" in keywords
        assert "routes" in keywords
        assert "to" not in keywords

    async def test_relevant_symbols_capped_at_10(self, in_memory_store: SymbolStore) -> None:
        """Relevant symbols list is capped at 10."""
        # Insert 15 symbols
        for i in range(15):
            in_memory_store.insert_symbol(
                name=f"func{i}", kind="function", language="python",
                file_path=f"src/mod{i}.py", line_number=1, end_line=10,
                is_exported=True, signature="() -> int", params=None,
                return_type="int", documentation=None, last_indexed_at=1000,
            )
        engine = BriefingEngine(in_memory_store, api_key=None)

        result = await engine.generate_briefing("func")

        assert isinstance(result, Ok)
        assert len(result.value.relevant_symbols) <= 10

    async def test_warnings_extraction(self, in_memory_store: SymbolStore) -> None:
        """WARNING: lines are extracted from AI response."""
        _populate_store(in_memory_store)
        engine = BriefingEngine(in_memory_store, api_key="test-key")

        ai_response = (
            "Use authMiddleware for route protection.\n"
            "WARNING: Don't import jwt functions from the barrel export.\n"
            "signToken handles token creation."
        )

        with patch.object(engine._client, "complete", new_callable=AsyncMock) as mock:
            mock.return_value = Ok((ai_response, 80))
            result = await engine.generate_briefing("add JWT auth")

        assert isinstance(result, Ok)
        br = result.value
        assert len(br.warnings) == 1
        assert "barrel export" in br.warnings[0]
        assert "WARNING:" not in br.briefing
