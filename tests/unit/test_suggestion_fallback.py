"""Tests for the enhanced suggestion fallback chain in ValidationOrchestrator."""

from __future__ import annotations

import time
from typing import Any


from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok
from groundtruth.validators.orchestrator import ValidationOrchestrator


def _setup_store() -> SymbolStore:
    """Create a store with symbols for fallback testing."""
    store = SymbolStore(":memory:")
    store.initialize()
    now = int(time.time())

    # getUserById in queries.py
    r1 = store.insert_symbol(
        name="getUserById",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=10,
        end_line=20,
        is_exported=True,
        signature="(user_id: int) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r1, Ok)
    store.insert_export(r1.value, "src/users/queries", is_default=False)

    # getUserByEmail — similar name
    r2 = store.insert_symbol(
        name="getUserByEmail",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=25,
        end_line=35,
        is_exported=True,
        signature="(email: str) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r2, Ok)
    store.insert_export(r2.value, "src/users/queries", is_default=False)

    # deleteUser — shares "User" component
    r3 = store.insert_symbol(
        name="deleteUser",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=40,
        end_line=50,
        is_exported=True,
        signature="(user_id: int) -> bool",
        params=None,
        return_type="bool",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r3, Ok)
    store.insert_export(r3.value, "src/users/queries", is_default=False)

    return store


class TestSuggestionFallbackChain:
    def test_levenshtein_match(self) -> None:
        """Levenshtein finds close name matches first."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        errors: list[dict[str, Any]] = [
            {
                "type": "symbol_not_found",
                "message": "getUserByld not found",  # typo: ld instead of Id
                "symbol_name": "getUserByld",
                "suggestion": None,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        assert enriched[0]["suggestion"] is not None
        assert "Levenshtein" in enriched[0]["suggestion"]["reason"]

    def test_component_match_fallback(self) -> None:
        """Component matching fires when Levenshtein has no close match."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        errors: list[dict[str, Any]] = [
            {
                "type": "symbol_not_found",
                "message": "fetchUserData not found",
                "symbol_name": "fetchUserData",  # shares "User" component
                "suggestion": None,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        # Should find component match with getUserById/getUserByEmail/deleteUser
        assert enriched[0]["suggestion"] is not None

    def test_module_export_listing(self) -> None:
        """Module export listing suggests available exports."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        errors: list[dict[str, Any]] = [
            {
                "type": "symbol_not_found",
                "message": "createUser not found in src/users/queries",
                "symbol_name": "createUser",  # doesn't exist — no close match
                "module_path": "src/users/queries",
                "suggestion": None,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        assert enriched[0]["suggestion"] is not None

    def test_cross_index_match(self) -> None:
        """Cross-index finds symbol at a different path."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        # getUserById exists but error has no close levenshtein or component match
        errors: list[dict[str, Any]] = [
            {
                "type": "wrong_module_path",
                "message": "getUserById not found in auth/",
                "symbol_name": "getUserById",
                "suggestion": None,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        assert enriched[0]["suggestion"] is not None
        # It should use Levenshtein (distance 0 = exact match)
        assert enriched[0]["suggestion"]["source"] == "deterministic"

    def test_chain_order(self) -> None:
        """Levenshtein takes priority over component matching."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        # "getUserByld" is 1 edit from "getUserById" — Levenshtein should win
        errors: list[dict[str, Any]] = [
            {
                "type": "symbol_not_found",
                "message": "getUserByld not found",
                "symbol_name": "getUserByld",
                "suggestion": None,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        assert "Levenshtein" in enriched[0]["suggestion"]["reason"]

    def test_skip_if_suggestion_exists(self) -> None:
        """Errors with existing suggestions are not re-enriched."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        existing_suggestion = {"source": "deterministic", "fix": "already fixed"}
        errors: list[dict[str, Any]] = [
            {
                "type": "symbol_not_found",
                "message": "test",
                "symbol_name": "getUserById",
                "suggestion": existing_suggestion,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        assert enriched[0]["suggestion"] == existing_suggestion

    def test_no_match_returns_no_suggestion(self) -> None:
        """When no matching strategy works, suggestion stays None."""
        store = _setup_store()
        orch = ValidationOrchestrator(store, lsp_manager=None, api_key=None)
        errors: list[dict[str, Any]] = [
            {
                "type": "symbol_not_found",
                "message": "zzzzzzzzzzz not found",
                "symbol_name": "zzzzzzzzzzz",
                "suggestion": None,
            }
        ]
        enriched = orch._enrich_with_suggestions(errors)
        assert enriched[0]["suggestion"] is None
