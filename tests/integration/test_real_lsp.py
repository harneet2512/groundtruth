"""Real LSP integration tests using pyright against the Python test fixture.

These tests spawn an actual pyright-langserver process and verify that
GroundTruth's LSP client, manager, and indexer work end-to-end with a
real language server.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from groundtruth.index.indexer import Indexer
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.manager import LSPManager
from groundtruth.utils.result import Ok

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "project_py"
QUERIES_FILE = str(FIXTURE_PATH / "src" / "users" / "queries.py")
ERRORS_FILE = str(FIXTURE_PATH / "src" / "utils" / "errors.py")
APP_FILE = str(FIXTURE_PATH / "src" / "app.py")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("pyright-langserver") is None,
        reason="pyright-langserver not found on PATH",
    ),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="Real LSP documentSymbol times out on Windows (pipe/event-loop); see PROGRESS.md",
    ),
    pytest.mark.timeout(30),
]


@pytest.fixture
async def lsp_env() -> tuple[SymbolStore, LSPManager, Indexer]:
    """Set up an in-memory store, LSP manager, and indexer for the Python fixture project."""
    store = SymbolStore(":memory:")
    init_result = store.initialize()
    assert isinstance(init_result, Ok), f"Store init failed: {init_result}"

    manager = LSPManager(str(FIXTURE_PATH), trace_dir=None)

    # Ensure the pyright server is running before tests
    server_result = await manager.ensure_server(".py")
    assert isinstance(server_result, Ok), f"LSP server start failed: {server_result}"

    indexer = Indexer(store, manager)

    yield store, manager, indexer  # type: ignore[misc]

    await manager.shutdown_all()


class TestSymbolsIndexed:
    @pytest.mark.asyncio
    async def test_symbols_indexed(self, lsp_env: tuple[SymbolStore, LSPManager, Indexer]) -> None:
        store, _manager, indexer = lsp_env

        result = await indexer.index_file(QUERIES_FILE)
        assert isinstance(result, Ok), f"index_file failed: {result}"
        assert result.value > 0, "Expected at least one symbol to be indexed"

        symbols_result = store.get_symbols_in_file(QUERIES_FILE)
        assert isinstance(symbols_result, Ok)
        symbols = symbols_result.value
        assert len(symbols) > 0

        symbol_names = {s.name for s in symbols}
        assert "get_user_by_id" in symbol_names
        assert "create_user" in symbol_names


class TestHoverReturnsTypes:
    @pytest.mark.asyncio
    async def test_hover_returns_types(
        self, lsp_env: tuple[SymbolStore, LSPManager, Indexer]
    ) -> None:
        store, _manager, indexer = lsp_env

        result = await indexer.index_file(QUERIES_FILE)
        assert isinstance(result, Ok)

        symbols_result = store.get_symbols_in_file(QUERIES_FILE)
        assert isinstance(symbols_result, Ok)

        # Find get_user_by_id and check that it has a signature from hover
        exported_functions = [
            s for s in symbols_result.value if s.name == "get_user_by_id" and s.is_exported
        ]
        assert len(exported_functions) == 1
        func = exported_functions[0]

        # The indexer fetches hover for exported symbols, so signature should be populated
        assert func.signature is not None, "Expected hover to populate the signature"
        assert len(func.signature) > 0


class TestCrossFileReferences:
    @pytest.mark.asyncio
    async def test_cross_file_references(
        self, lsp_env: tuple[SymbolStore, LSPManager, Indexer]
    ) -> None:
        store, _manager, indexer = lsp_env

        # Index both the defining file and a file that imports from it
        errors_result = await indexer.index_file(ERRORS_FILE)
        assert isinstance(errors_result, Ok)

        queries_result = await indexer.index_file(QUERIES_FILE)
        assert isinstance(queries_result, Ok)

        app_result = await indexer.index_file(APP_FILE)
        assert isinstance(app_result, Ok)

        # Look up get_user_by_id and check for cross-file references
        symbols_result = store.find_symbol_by_name("get_user_by_id")
        assert isinstance(symbols_result, Ok)
        assert len(symbols_result.value) > 0

        symbol = symbols_result.value[0]
        refs_result = store.get_refs_for_symbol(symbol.id)
        assert isinstance(refs_result, Ok)

        # app.py imports get_user_by_id, so there should be at least one cross-file ref
        ref_files = {r.referenced_in_file for r in refs_result.value}
        assert len(ref_files) > 0, "Expected at least one cross-file reference for get_user_by_id"


class TestPartialIndexOnFailure:
    @pytest.mark.asyncio
    async def test_partial_index_on_failure(
        self, lsp_env: tuple[SymbolStore, LSPManager, Indexer], tmp_path: Path
    ) -> None:
        store, manager, indexer = lsp_env

        # Create a file with a syntax error
        bad_file = tmp_path / "bad_syntax.py"
        bad_file.write_text("def broken(\n    this is not valid python\n", encoding="utf-8")

        # Create an indexer pointing at tmp_path so pyright can see the bad file
        tmp_manager = LSPManager(str(tmp_path), trace_dir=None)
        server_result = await tmp_manager.ensure_server(".py")
        assert isinstance(server_result, Ok)

        tmp_store = SymbolStore(":memory:")
        tmp_store.initialize()
        tmp_indexer = Indexer(tmp_store, tmp_manager)

        # Write a valid file next to the bad one
        good_file = tmp_path / "good_module.py"
        good_file.write_text(
            "def hello_world() -> str:\n    return 'hello'\n",
            encoding="utf-8",
        )

        # Index the bad file -- may fail or return 0 symbols, but must not raise
        await tmp_indexer.index_file(str(bad_file))
        # We don't assert Ok here -- syntax errors may cause LSP to return
        # zero symbols or an error, both are acceptable

        # Index the good file -- must succeed despite the bad file existing
        good_result = await tmp_indexer.index_file(str(good_file))
        assert isinstance(good_result, Ok), (
            f"Good file indexing should succeed even after bad file: {good_result}"
        )
        assert good_result.value > 0

        good_symbols = tmp_store.get_symbols_in_file(str(good_file))
        assert isinstance(good_symbols, Ok)
        good_names = {s.name for s in good_symbols.value}
        assert "hello_world" in good_names

        await tmp_manager.shutdown_all()
