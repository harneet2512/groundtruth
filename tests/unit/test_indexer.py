"""Tests for the Indexer (LSP → SQLite orchestration)."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from groundtruth.index.indexer import (
    IGNORE_DIRS,
    Indexer,
    is_exported,
    parse_hover_signature,
    symbol_kind_to_str,
)
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.client import LSPClient
from groundtruth.lsp.manager import LSPManager
from groundtruth.lsp.protocol import (
    DocumentSymbol,
    Hover,
    Location,
    MarkupContent,
    Position,
    Range,
    SymbolKind,
)
from groundtruth.utils.result import Err, Ok


def _make_range(sl: int = 0, sc: int = 0, el: int = 0, ec: int = 0) -> Range:
    return Range(start=Position(line=sl, character=sc), end=Position(line=el, character=ec))


def _make_symbol(
    name: str,
    kind: SymbolKind = SymbolKind.FUNCTION,
    sl: int = 0,
    el: int = 10,
    children: list[DocumentSymbol] | None = None,
    detail: str | None = None,
) -> DocumentSymbol:
    return DocumentSymbol(
        name=name,
        kind=kind,
        range=_make_range(sl, 0, el, 0),
        selection_range=_make_range(sl, 0, sl, len(name)),
        children=children,
        detail=detail,
    )


@pytest.fixture
def store() -> SymbolStore:
    s = SymbolStore(":memory:")
    s.initialize()
    return s


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock(spec=LSPClient)
    client.is_running = True
    client.did_open = AsyncMock()
    client.did_close = AsyncMock()
    client.drain = AsyncMock()
    client.document_symbol = AsyncMock(return_value=Ok([]))
    client.hover = AsyncMock(return_value=Ok(None))
    client.references = AsyncMock(return_value=Ok([]))
    return client


@pytest.fixture
def mock_manager(mock_client: AsyncMock) -> AsyncMock:
    manager = AsyncMock(spec=LSPManager)
    manager.ensure_server = AsyncMock(return_value=Ok(mock_client))
    return manager


class TestSymbolKindMapping:
    def test_symbol_kind_to_str(self) -> None:
        """SymbolKind → string mapping."""
        assert symbol_kind_to_str(SymbolKind.FUNCTION) == "function"
        assert symbol_kind_to_str(SymbolKind.CLASS) == "class"
        assert symbol_kind_to_str(SymbolKind.METHOD) == "method"
        assert symbol_kind_to_str(SymbolKind.VARIABLE) == "variable"
        assert symbol_kind_to_str(SymbolKind.INTERFACE) == "interface"
        assert symbol_kind_to_str(SymbolKind.ENUM) == "enum"
        assert symbol_kind_to_str(SymbolKind.PROPERTY) == "property"
        assert symbol_kind_to_str(SymbolKind.CONSTANT) == "constant"


class TestIsExported:
    def test_python_public(self) -> None:
        sym = _make_symbol("public_func")
        assert is_exported(sym, "python") is True

    def test_python_private(self) -> None:
        sym = _make_symbol("_private_func")
        assert is_exported(sym, "python") is False

    def test_go_exported(self) -> None:
        sym = _make_symbol("GetUser")
        assert is_exported(sym, "go") is True

    def test_go_unexported(self) -> None:
        sym = _make_symbol("getUser")
        assert is_exported(sym, "go") is False

    def test_typescript_default(self) -> None:
        sym = _make_symbol("anything")
        assert is_exported(sym, "typescript") is True


class TestParseHoverSignature:
    def test_markdown_code_block(self) -> None:
        hover = Hover(
            contents=MarkupContent(
                kind="markdown",
                value="```python\ndef foo(x: int) -> str\n```",
            )
        )
        sig, _params, ret = parse_hover_signature(hover)
        assert sig == "def foo(x: int) -> str"
        assert ret == "str"

    def test_plain_string(self) -> None:
        hover = Hover(contents="(x: int) -> bool")
        sig, _params, ret = parse_hover_signature(hover)
        assert sig == "(x: int) -> bool"
        assert ret == "bool"

    def test_empty_hover(self) -> None:
        hover = Hover(contents=MarkupContent(kind="plaintext", value=""))
        sig, params, ret = parse_hover_signature(hover)
        assert sig is None
        assert params is None
        assert ret is None


class TestIndexFile:
    @pytest.mark.asyncio
    async def test_index_file_extracts_symbols_lsp(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Mock LSP → verify symbols stored (uses .ts to exercise LSP path)."""
        symbols = [
            _make_symbol("myFunction", SymbolKind.FUNCTION, 0, 10),
            _make_symbol("MyClass", SymbolKind.CLASS, 12, 30),
        ]
        mock_client.document_symbol = AsyncMock(return_value=Ok(symbols))

        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False) as f:
            f.write("function myFunction() {}\nclass MyClass {}\n")
            tmp_path = f.name

        try:
            result = await indexer.index_file(tmp_path)
            assert isinstance(result, Ok)
            assert result.value == 2

            names_result = store.get_all_symbol_names()
            assert isinstance(names_result, Ok)
            assert sorted(names_result.value) == ["MyClass", "myFunction"]
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_index_file_tracks_references(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Verify refs created for exported symbols (uses .ts for LSP path)."""
        symbols = [_make_symbol("exported_fn", SymbolKind.FUNCTION, 0, 10)]
        mock_client.document_symbol = AsyncMock(return_value=Ok(symbols))
        mock_client.references = AsyncMock(return_value=Ok([
            Location(
                uri="file:///other/file.ts",
                range=_make_range(5, 0, 5, 10),
            ),
        ]))

        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False) as f:
            f.write("export function exported_fn() {}\n")
            tmp_path = f.name

        try:
            result = await indexer.index_file(tmp_path)
            assert isinstance(result, Ok)

            # Find the symbol and check refs
            sym_result = store.find_symbol_by_name("exported_fn")
            assert isinstance(sym_result, Ok)
            assert len(sym_result.value) == 1
            sid = sym_result.value[0].id

            refs_result = store.get_refs_for_symbol(sid)
            assert isinstance(refs_result, Ok)
            assert len(refs_result.value) >= 1
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_index_file_extracts_hover(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Verify signature/docs from hover (uses .ts for LSP path)."""
        symbols = [_make_symbol("typed_fn", SymbolKind.FUNCTION, 0, 5)]
        mock_client.document_symbol = AsyncMock(return_value=Ok(symbols))
        mock_client.hover = AsyncMock(return_value=Ok(Hover(
            contents=MarkupContent(
                kind="markdown",
                value="```typescript\nfunction typed_fn(x: number): string\n```",
            )
        )))

        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False) as f:
            f.write("function typed_fn(x: number): string { return ''; }\n")
            tmp_path = f.name

        try:
            await indexer.index_file(tmp_path)
            sym_result = store.find_symbol_by_name("typed_fn")
            assert isinstance(sym_result, Ok)
            s = sym_result.value[0]
            assert s.signature is not None
            assert "typed_fn" in s.signature
            assert s.return_type == "string"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_index_file_handles_nested_symbols(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Class with methods should store all symbols (uses .ts for LSP path)."""
        method = _make_symbol("do_thing", SymbolKind.METHOD, 5, 10)
        cls = _make_symbol("MyClass", SymbolKind.CLASS, 0, 20, children=[method])
        mock_client.document_symbol = AsyncMock(return_value=Ok([cls]))

        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False) as f:
            f.write("class MyClass {\n    do_thing() {}\n}\n")
            tmp_path = f.name

        try:
            result = await indexer.index_file(tmp_path)
            assert isinstance(result, Ok)
            assert result.value == 2

            names_result = store.get_all_symbol_names()
            assert isinstance(names_result, Ok)
            assert sorted(names_result.value) == ["MyClass", "do_thing"]
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_index_file_unsupported_extension(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Unsupported extension returns Err."""
        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".xyz", mode="w", delete=False) as f:
            f.write("something")
            tmp_path = f.name

        try:
            result = await indexer.index_file(tmp_path)
            assert isinstance(result, Err)
            assert result.error.code == "unsupported_language"
        finally:
            os.unlink(tmp_path)


class TestIndexProject:
    @pytest.mark.asyncio
    async def test_index_project_walks_directory(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Walk directory + index Python files."""
        symbols = [_make_symbol("proj_func", SymbolKind.FUNCTION)]
        mock_client.document_symbol = AsyncMock(return_value=Ok(symbols))

        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create source files
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "main.py"), "w") as f:
                f.write("def proj_func(): pass\n")
            with open(os.path.join(src_dir, "readme.md"), "w") as f:
                f.write("# readme\n")

            result = await indexer.index_project(tmpdir)
            assert isinstance(result, Ok)
            assert result.value >= 1

    @pytest.mark.asyncio
    async def test_index_project_skips_ignored_dirs(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """node_modules and __pycache__ should be skipped."""
        mock_client.document_symbol = AsyncMock(return_value=Ok([
            _make_symbol("should_index", SymbolKind.FUNCTION),
        ]))

        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Source file that should be indexed
            with open(os.path.join(tmpdir, "app.py"), "w") as f:
                f.write("def should_index(): pass\n")

            # Files in ignored directories
            nm_dir = os.path.join(tmpdir, "node_modules", "pkg")
            os.makedirs(nm_dir)
            with open(os.path.join(nm_dir, "index.js"), "w") as f:
                f.write("module.exports = {}\n")

            cache_dir = os.path.join(tmpdir, "__pycache__")
            os.makedirs(cache_dir)
            with open(os.path.join(cache_dir, "app.cpython-311.pyc"), "w") as f:
                f.write("")

            result = await indexer.index_project(tmpdir)
            assert isinstance(result, Ok)
            # Only app.py should be indexed (1 symbol)
            assert result.value == 1

    @pytest.mark.asyncio
    async def test_index_project_parses_package_json(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Parse package.json for npm packages."""
        mock_client.document_symbol = AsyncMock(return_value=Ok([]))
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_json = {
                "dependencies": {"express": "^4.18.0"},
                "devDependencies": {"jest": "^29.0.0"},
            }
            with open(os.path.join(tmpdir, "package.json"), "w") as f:
                json.dump(pkg_json, f)

            await indexer.index_project(tmpdir)

            express = store.get_package("express")
            assert isinstance(express, Ok)
            assert express.value is not None
            assert express.value.package_manager == "npm"
            assert express.value.is_dev_dependency is False

            jest = store.get_package("jest")
            assert isinstance(jest, Ok)
            assert jest.value is not None
            assert jest.value.is_dev_dependency is True

    @pytest.mark.asyncio
    async def test_index_project_parses_requirements_txt(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Parse requirements.txt for pip packages."""
        mock_client.document_symbol = AsyncMock(return_value=Ok([]))
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "requirements.txt"), "w") as f:
                f.write("requests==2.31.0\nflask>=2.0\nclick\n")

            await indexer.index_project(tmpdir)

            req = store.get_package("requests")
            assert isinstance(req, Ok)
            assert req.value is not None
            assert req.value.version == "2.31.0"
            assert req.value.package_manager == "pip"

            click = store.get_package("click")
            assert isinstance(click, Ok)
            assert click.value is not None
            assert click.value.version is None


class TestDiscoverFiles:
    def test_discover_files_git(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """git ls-files output is parsed correctly."""
        indexer = Indexer(store, mock_manager)
        # Mock _can_index to always return True for .py
        indexer._server_available = {".py": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create actual files so getsize works
            os.makedirs(os.path.join(tmpdir, "src"))
            with open(os.path.join(tmpdir, "src", "main.py"), "w") as f:
                f.write("x = 1\n")
            with open(os.path.join(tmpdir, "src", "utils.py"), "w") as f:
                f.write("y = 2\n")

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = b"src/main.py\0src/utils.py\0"

            with patch("subprocess.run", return_value=mock_result):
                files = indexer._discover_files(tmpdir, max_file_size=1_048_576)

            assert len(files) == 2
            assert any("main.py" in f for f in files)
            assert any("utils.py" in f for f in files)

    def test_discover_files_fallback(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """When git is unavailable, falls back to os.walk."""
        indexer = Indexer(store, mock_manager)
        indexer._server_available = {".py": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "app.py"), "w") as f:
                f.write("x = 1\n")

            with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
                files = indexer._discover_files(tmpdir, max_file_size=1_048_576)

            assert len(files) == 1
            assert any("app.py" in f for f in files)


class TestCanIndex:
    def test_can_index_caches(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """shutil.which is called once per extension, then cached."""
        indexer = Indexer(store, mock_manager)

        with patch("shutil.which", return_value="/usr/bin/pyright-langserver") as mock_which:
            result1 = indexer._can_index(".py")
            result2 = indexer._can_index(".py")

        assert result1 is True
        assert result2 is True
        assert mock_which.call_count == 1

    def test_can_index_warns_once(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Only one warning per missing LSP server."""
        indexer = Indexer(store, mock_manager)

        with patch("shutil.which", return_value=None):
            indexer._can_index(".py")
            indexer._can_index(".py")

        assert ".py" in indexer._warned_extensions


class TestIsIndexable:
    def test_skips_binary(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Binary extensions are skipped."""
        indexer = Indexer(store, mock_manager)

        assert indexer._is_indexable("image.png", ".png") is False
        assert indexer._is_indexable("app.exe", ".exe") is False
        assert indexer._is_indexable("data.json", ".json") is False
        assert indexer._is_indexable("readme.md", ".md") is False


class TestReadFileSafe:
    def test_utf8(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Normal UTF-8 file is read correctly."""
        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write("x = 'hello'\n")
            tmp_path = f.name

        try:
            result = indexer._read_file_safe(tmp_path)
            assert isinstance(result, Ok)
            assert "hello" in result.value
        finally:
            os.unlink(tmp_path)

    def test_latin1_fallback(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Non-UTF8 file falls back to latin-1."""
        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".py", delete=False) as f:
            f.write(b"x = '\xe9'\n")  # é in latin-1
            tmp_path = f.name

        try:
            result = indexer._read_file_safe(tmp_path)
            assert isinstance(result, Ok)
            assert "é" in result.value
        finally:
            os.unlink(tmp_path)

    def test_permission_error(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Permission error returns Err gracefully."""
        indexer = Indexer(store, mock_manager)

        with patch("builtins.open", side_effect=PermissionError("access denied")):
            result = indexer._read_file_safe("/some/file.py")
            assert isinstance(result, Err)
            assert "Permission denied" in result.error.message


class TestExcludeDirs:
    def test_exclude_dirs_passed_to_constructor(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """exclude_dirs does not mutate module-level IGNORE_DIRS."""
        original_ignore = IGNORE_DIRS.copy()

        indexer = Indexer(store, mock_manager, exclude_dirs={"custom_vendor", "generated"})

        assert indexer._exclude_dirs == {"custom_vendor", "generated"}
        assert IGNORE_DIRS == original_ignore  # Module-level set unchanged


class TestGroundtruthIgnore:
    def test_groundtruthignore_loaded(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """Patterns from .groundtruthignore are loaded."""
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, ".groundtruthignore"), "w") as f:
                f.write("# comment\ngenerated/\n*.bak\n")

            patterns = indexer._load_ignore_patterns(tmpdir)

        assert "generated/" in patterns
        assert "*.bak" in patterns
        assert "# comment" not in patterns


class TestIndexBatch:
    @pytest.mark.asyncio
    async def test_index_batch_opens_all_then_queries(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Batch indexing: didOpen all files first, then documentSymbol per file."""
        symbols = [_make_symbol("batch_fn", SymbolKind.FUNCTION)]
        mock_client.document_symbol = AsyncMock(return_value=Ok(symbols))

        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for i in range(3):
                fp = os.path.join(tmpdir, f"file{i}.py")
                with open(fp, "w") as f:
                    f.write(f"def batch_fn(): pass\n")
                paths.append(fp)

            results = await indexer._index_batch(paths, mock_client, "python")

        assert len(results) == 3
        for _fp, result in results:
            assert isinstance(result, Ok)
            assert result.value == 1

        # Verify all files were opened before any were closed
        assert mock_client.did_open.call_count == 3
        assert mock_client.drain.call_count == 1
        assert mock_client.document_symbol.call_count == 3
        assert mock_client.did_close.call_count == 3

    @pytest.mark.asyncio
    async def test_index_batch_skips_poison_files(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Poison files are skipped in batch indexing."""
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            good_fp = os.path.join(tmpdir, "good.py")
            with open(good_fp, "w") as f:
                f.write("x = 1\n")
            bad_fp = os.path.join(tmpdir, "bad.py")
            with open(bad_fp, "w") as f:
                f.write("x = 2\n")

            indexer._poison_files.add(bad_fp)
            mock_client.document_symbol = AsyncMock(return_value=Ok([]))

            results = await indexer._index_batch([good_fp, bad_fp], mock_client, "python")

        assert len(results) == 2
        # good.py succeeded, bad.py was skipped
        good_result = [r for fp, r in results if fp == good_fp][0]
        bad_result = [r for fp, r in results if fp == bad_fp][0]
        assert isinstance(good_result, Ok)
        assert isinstance(bad_result, Err)
        assert bad_result.error.code == "poison_file"

    @pytest.mark.asyncio
    async def test_index_batch_handles_symbol_failure(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """If documentSymbol fails for one file, others still succeed."""
        from groundtruth.utils.result import GroundTruthError

        call_count = 0

        async def symbol_side_effect(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return Err(GroundTruthError(code="lsp_timeout", message="timed out"))
            return Ok([_make_symbol("fn", SymbolKind.FUNCTION)])

        mock_client.document_symbol = AsyncMock(side_effect=symbol_side_effect)
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for i in range(3):
                fp = os.path.join(tmpdir, f"f{i}.py")
                with open(fp, "w") as f:
                    f.write("def fn(): pass\n")
                paths.append(fp)

            results = await indexer._index_batch(paths, mock_client, "python")

        ok_count = sum(1 for _, r in results if isinstance(r, Ok))
        err_count = sum(1 for _, r in results if isinstance(r, Err))
        assert ok_count == 2
        assert err_count == 1


class TestIndexProjectGitIntegration:
    @pytest.mark.asyncio
    async def test_index_project_uses_git_ls_files(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """index_project uses git ls-files when available."""
        symbols = [_make_symbol("git_func", SymbolKind.FUNCTION)]
        mock_client.document_symbol = AsyncMock(return_value=Ok(symbols))

        indexer = Indexer(store, mock_manager)
        indexer._server_available = {".py": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the file on disk
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("def git_func(): pass\n")

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = b"main.py\0"

            with patch("subprocess.run", return_value=mock_result) as mock_run:
                result = await indexer.index_project(tmpdir)

            assert isinstance(result, Ok)
            # Verify git ls-files was called
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert "ls-files" in call_args[0][0]


class TestASTIndexing:
    def test_is_indexable_py_without_lsp(
        self, store: SymbolStore, mock_manager: AsyncMock
    ) -> None:
        """.py returns True even when shutil.which returns None (AST path)."""
        indexer = Indexer(store, mock_manager)

        with patch("shutil.which", return_value=None):
            result = indexer._is_indexable("test.py", ".py")

        assert result is True

    @pytest.mark.asyncio
    async def test_index_file_python_uses_ast(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Python files go through AST, not LSP."""
        indexer = Indexer(store, mock_manager)

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("def hello(name: str) -> str:\n    '''Greet.'''\n    return name\n")
            tmp_path = f.name

        try:
            result = await indexer.index_file(tmp_path)
            assert isinstance(result, Ok)
            assert result.value == 1

            # Verify symbol was stored correctly
            sym_result = store.find_symbol_by_name("hello")
            assert isinstance(sym_result, Ok)
            assert len(sym_result.value) == 1
            s = sym_result.value[0]
            assert s.kind == "function"
            assert s.language == "python"
            assert s.signature is not None
            assert "name: str" in s.signature
            assert s.return_type == "str"
            assert s.documentation == "Greet."

            # Verify LSP was NOT called
            mock_manager.ensure_server.assert_not_called()
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_index_file_python_with_class(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """Python class + methods indexed via AST."""
        indexer = Indexer(store, mock_manager)
        code = (
            "class MyService:\n"
            "    def process(self, data: list) -> bool:\n"
            "        return True\n"
        )

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp_path = f.name

        try:
            result = await indexer.index_file(tmp_path)
            assert isinstance(result, Ok)
            assert result.value == 2  # class + method

            names_result = store.get_all_symbol_names()
            assert isinstance(names_result, Ok)
            assert sorted(names_result.value) == ["MyService", "process"]
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_index_project_uses_ast_for_python(
        self, store: SymbolStore, mock_client: AsyncMock, mock_manager: AsyncMock
    ) -> None:
        """index_project routes Python files through AST, not LSP."""
        indexer = Indexer(store, mock_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "app.py"), "w") as f:
                f.write("def main() -> None:\n    '''Entry point.'''\n    pass\n")
            with open(os.path.join(tmpdir, "utils.py"), "w") as f:
                f.write("def helper() -> int:\n    return 42\n")

            with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
                result = await indexer.index_project(tmpdir)

            assert isinstance(result, Ok)
            assert result.value >= 2

            # Both functions should be in the store
            main_result = store.find_symbol_by_name("main")
            assert isinstance(main_result, Ok)
            assert len(main_result.value) == 1
            helper_result = store.find_symbol_by_name("helper")
            assert isinstance(helper_result, Ok)
            assert len(helper_result.value) == 1

            # LSP ensure_server should NOT be called for .py
            # (it may be called 0 times if only Python files exist)
            for call in mock_manager.ensure_server.call_args_list:
                assert call[0][0] != ".py"
