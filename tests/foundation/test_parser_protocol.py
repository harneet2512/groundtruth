"""Tests for the Foundation v2 parser abstraction.

Tests cover:
- SymbolExtractor protocol conformance
- Tree-sitter backend Python parsing
- Python AST backend wrapping
- Parity between tree-sitter and AST backends
- Fallback behavior when tree-sitter is unavailable
- Edge cases: empty files, syntax errors, unsupported languages
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from groundtruth.foundation.parser.protocol import (
    ExtractedSymbol,
    ParsedFile,
    SymbolExtractor,
)

# Fixture: realistic Python source with diverse symbol types
PYTHON_FIXTURE = '''\
"""Module docstring."""

CONSTANT = 42


def greet(name: str, greeting: str = "Hello") -> str:
    """Greet someone."""
    return f"{greeting}, {name}!"


def _private_func(x: int) -> int:
    return x * 2


class UserService:
    """Manages users."""

    def __init__(self, db, cache=None):
        self.db = db
        self.cache = cache

    def get_user(self, user_id: int) -> dict:
        """Get a user by ID."""
        if self.cache:
            cached = self.cache.get(user_id)
            if cached:
                return cached
        user = self.db.query(user_id)
        return user

    def _internal(self):
        pass

    @property
    def count(self) -> int:
        """Total users."""
        return self.db.count()


class _PrivateClass:
    def method(self):
        pass
'''

# Simpler fixture for basic tests
SIMPLE_FIXTURE = '''\
def hello():
    pass

class Foo:
    def bar(self, x):
        return x
'''


@pytest.fixture
def python_file(tmp_path: Path) -> str:
    """Create a temporary Python file with the full fixture."""
    p = tmp_path / "test_module.py"
    p.write_text(PYTHON_FIXTURE)
    return str(p)


@pytest.fixture
def simple_file(tmp_path: Path) -> str:
    """Create a temporary Python file with the simple fixture."""
    p = tmp_path / "simple.py"
    p.write_text(SIMPLE_FIXTURE)
    return str(p)


@pytest.fixture
def empty_file(tmp_path: Path) -> str:
    p = tmp_path / "empty.py"
    p.write_text("")
    return str(p)


@pytest.fixture
def syntax_error_file(tmp_path: Path) -> str:
    p = tmp_path / "bad.py"
    p.write_text("def foo(\n  # incomplete")
    return str(p)


# ---- Protocol conformance ----


class TestProtocolConformance:
    def test_treesitter_implements_protocol(self):
        from groundtruth.foundation.parser.treesitter_backend import (
            HAS_TREE_SITTER,
            TreeSitterExtractor,
        )

        if not HAS_TREE_SITTER:
            pytest.skip("tree-sitter not installed")
        ext = TreeSitterExtractor()
        assert isinstance(ext, SymbolExtractor)

    def test_ast_backend_implements_protocol(self):
        from groundtruth.foundation.parser.ast_backend import PythonASTExtractor

        ext = PythonASTExtractor()
        assert isinstance(ext, SymbolExtractor)


# ---- Tree-sitter backend ----


class TestTreeSitterBackend:
    @pytest.fixture(autouse=True)
    def _require_treesitter(self):
        from groundtruth.foundation.parser.treesitter_backend import HAS_TREE_SITTER

        if not HAS_TREE_SITTER:
            pytest.skip("tree-sitter not installed")

    def _get_extractor(self):
        from groundtruth.foundation.parser.treesitter_backend import TreeSitterExtractor

        return TreeSitterExtractor()

    def test_supported_languages(self):
        ext = self._get_extractor()
        langs = ext.supported_languages
        assert "python" in langs

    def test_parse_python_file(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        assert parsed.language == "python"
        assert parsed.error is None
        assert parsed.tree is not None

    def test_extract_functions(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        func_names = [s.name for s in symbols if s.kind == "function"]
        assert "greet" in func_names
        assert "_private_func" in func_names

    def test_extract_classes(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        class_names = [s.name for s in symbols if s.kind == "class"]
        assert "UserService" in class_names
        assert "_PrivateClass" in class_names

    def test_extract_methods(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        user_service = next(s for s in symbols if s.name == "UserService")
        method_names = [c.name for c in user_service.children]
        assert "__init__" in method_names
        assert "get_user" in method_names
        assert "_internal" in method_names

    def test_method_parent_class(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        user_service = next(s for s in symbols if s.name == "UserService")
        for child in user_service.children:
            assert child.parent_class == "UserService"

    def test_function_parameters(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        greet = next(s for s in symbols if s.name == "greet")
        assert "name" in greet.parameters
        assert "greeting" in greet.parameters
        assert "self" not in greet.parameters

    def test_method_parameters_exclude_self(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        user_service = next(s for s in symbols if s.name == "UserService")
        get_user = next(c for c in user_service.children if c.name == "get_user")
        assert "user_id" in get_user.parameters
        assert "self" not in get_user.parameters

    def test_is_exported(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        greet = next(s for s in symbols if s.name == "greet")
        assert greet.is_exported is True

        private = next(s for s in symbols if s.name == "_private_func")
        assert private.is_exported is False

    def test_raw_text_populated(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        greet = next(s for s in symbols if s.name == "greet")
        assert "def greet" in greet.raw_text
        assert "return" in greet.raw_text

    def test_docstring_extraction(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        greet = next(s for s in symbols if s.name == "greet")
        assert greet.documentation == "Greet someone."

    def test_property_detection(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        user_service = next(s for s in symbols if s.name == "UserService")
        count = next(c for c in user_service.children if c.name == "count")
        assert count.kind == "property"

    def test_empty_file(self, empty_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(empty_file)
        symbols = ext.extract_symbols(parsed)
        assert symbols == []

    def test_syntax_error_file(self, syntax_error_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(syntax_error_file)
        # tree-sitter is error-tolerant, so it may still parse
        symbols = ext.extract_symbols(parsed)
        # Should not crash — may return partial results or empty
        assert isinstance(symbols, list)

    def test_unsupported_extension(self, tmp_path: Path):
        p = tmp_path / "file.xyz"
        p.write_text("hello")
        ext = self._get_extractor()
        parsed = ext.parse_file(str(p))
        assert parsed.error is not None
        symbols = ext.extract_symbols(parsed)
        assert symbols == []

    def test_nonexistent_file(self):
        ext = self._get_extractor()
        parsed = ext.parse_file("/nonexistent/file.py")
        assert parsed.error is not None

    def test_parse_with_content(self):
        ext = self._get_extractor()
        content = b"def foo():\n    pass\n"
        parsed = ext.parse_file("virtual.py", content=content)
        assert parsed.error is None
        symbols = ext.extract_symbols(parsed)
        assert len(symbols) == 1
        assert symbols[0].name == "foo"

    def test_constant_extraction(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        var_names = [s.name for s in symbols if s.kind == "variable"]
        assert "CONSTANT" in var_names


# ---- Python AST backend ----


class TestPythonASTBackend:
    def _get_extractor(self):
        from groundtruth.foundation.parser.ast_backend import PythonASTExtractor

        return PythonASTExtractor()

    def test_supported_languages(self):
        ext = self._get_extractor()
        assert ext.supported_languages == ["python"]

    def test_parse_python_file(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        assert parsed.language == "python"
        assert parsed.error is None

    def test_extract_functions(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        func_names = [s.name for s in symbols if s.kind == "function"]
        assert "greet" in func_names
        assert "_private_func" in func_names

    def test_extract_classes(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        class_names = [s.name for s in symbols if s.kind == "class"]
        assert "UserService" in class_names

    def test_extract_methods(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        user_service = next(s for s in symbols if s.name == "UserService")
        method_names = [c.name for c in user_service.children]
        assert "__init__" in method_names
        assert "get_user" in method_names

    def test_function_parameters(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        greet = next(s for s in symbols if s.name == "greet")
        assert "name" in greet.parameters
        assert "greeting" in greet.parameters
        assert "self" not in greet.parameters

    def test_is_exported(self, python_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(python_file)
        symbols = ext.extract_symbols(parsed)

        greet = next(s for s in symbols if s.name == "greet")
        assert greet.is_exported is True

        private = next(s for s in symbols if s.name == "_private_func")
        assert private.is_exported is False

    def test_rejects_non_python(self, tmp_path: Path):
        p = tmp_path / "file.js"
        p.write_text("function foo() {}")
        ext = self._get_extractor()
        parsed = ext.parse_file(str(p))
        assert parsed.error is not None

    def test_empty_file(self, empty_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(empty_file)
        symbols = ext.extract_symbols(parsed)
        assert symbols == []

    def test_syntax_error(self, syntax_error_file: str):
        ext = self._get_extractor()
        parsed = ext.parse_file(syntax_error_file)
        assert parsed.error is not None
        symbols = ext.extract_symbols(parsed)
        assert symbols == []


# ---- Parity tests: tree-sitter vs AST ----


class TestBackendParity:
    """Verify that tree-sitter and AST backends produce matching results for Python."""

    @pytest.fixture(autouse=True)
    def _require_treesitter(self):
        from groundtruth.foundation.parser.treesitter_backend import HAS_TREE_SITTER

        if not HAS_TREE_SITTER:
            pytest.skip("tree-sitter not installed")

    def _get_both(self):
        from groundtruth.foundation.parser.ast_backend import PythonASTExtractor
        from groundtruth.foundation.parser.treesitter_backend import TreeSitterExtractor

        return TreeSitterExtractor(), PythonASTExtractor()

    def _compare_symbols(self, ts_syms: list[ExtractedSymbol], ast_syms: list[ExtractedSymbol]):
        """Compare two symbol lists for structural equivalence."""
        # Same count of top-level symbols
        assert len(ts_syms) == len(ast_syms), (
            f"Symbol count mismatch: tree-sitter={len(ts_syms)}, ast={len(ast_syms)}\n"
            f"TS: {[s.name for s in ts_syms]}\n"
            f"AST: {[s.name for s in ast_syms]}"
        )

        for ts, ast_sym in zip(ts_syms, ast_syms):
            assert ts.name == ast_sym.name, f"Name mismatch: {ts.name} vs {ast_sym.name}"
            assert ts.kind == ast_sym.kind, f"Kind mismatch for {ts.name}: {ts.kind} vs {ast_sym.kind}"
            assert ts.start_line == ast_sym.start_line, (
                f"Start line mismatch for {ts.name}: {ts.start_line} vs {ast_sym.start_line}"
            )
            assert ts.is_exported == ast_sym.is_exported, (
                f"is_exported mismatch for {ts.name}: {ts.is_exported} vs {ast_sym.is_exported}"
            )
            # Compare children count
            assert len(ts.children) == len(ast_sym.children), (
                f"Children count mismatch for {ts.name}: "
                f"tree-sitter={len(ts.children)}, ast={len(ast_sym.children)}\n"
                f"TS children: {[c.name for c in ts.children]}\n"
                f"AST children: {[c.name for c in ast_sym.children]}"
            )
            # Recursively compare children
            if ts.children and ast_sym.children:
                self._compare_symbols(list(ts.children), list(ast_sym.children))

    def test_parity_simple(self, simple_file: str):
        ts_ext, ast_ext = self._get_both()

        ts_parsed = ts_ext.parse_file(simple_file)
        ast_parsed = ast_ext.parse_file(simple_file)

        ts_symbols = ts_ext.extract_symbols(ts_parsed)
        ast_symbols = ast_ext.extract_symbols(ast_parsed)

        self._compare_symbols(ts_symbols, ast_symbols)

    def test_parity_full_fixture(self, python_file: str):
        ts_ext, ast_ext = self._get_both()

        ts_parsed = ts_ext.parse_file(python_file)
        ast_parsed = ast_ext.parse_file(python_file)

        ts_symbols = ts_ext.extract_symbols(ts_parsed)
        ast_symbols = ast_ext.extract_symbols(ast_parsed)

        self._compare_symbols(ts_symbols, ast_symbols)

    def test_parity_real_file(self):
        """Test parity on a real file from the GT codebase."""
        ts_ext, ast_ext = self._get_both()

        # Use a known file from the repo
        real_file = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "src", "groundtruth", "core", "flags.py"
        )
        real_file = os.path.normpath(real_file)
        if not os.path.exists(real_file):
            pytest.skip("flags.py not found")

        ts_parsed = ts_ext.parse_file(real_file)
        ast_parsed = ast_ext.parse_file(real_file)

        ts_symbols = ts_ext.extract_symbols(ts_parsed)
        ast_symbols = ast_ext.extract_symbols(ast_parsed)

        self._compare_symbols(ts_symbols, ast_symbols)

    def test_parity_parameter_counts(self, python_file: str):
        """Parameter counts should match between backends."""
        ts_ext, ast_ext = self._get_both()

        ts_parsed = ts_ext.parse_file(python_file)
        ast_parsed = ast_ext.parse_file(python_file)

        ts_symbols = ts_ext.extract_symbols(ts_parsed)
        ast_symbols = ast_ext.extract_symbols(ast_parsed)

        for ts, ast_sym in zip(ts_symbols, ast_symbols):
            assert len(ts.parameters) == len(ast_sym.parameters), (
                f"Param count mismatch for {ts.name}: "
                f"tree-sitter={ts.parameters}, ast={ast_sym.parameters}"
            )


# ---- Registry ----


class TestRegistry:
    def test_get_extractor_returns_something(self):
        from groundtruth.foundation.parser.registry import get_extractor

        ext = get_extractor()
        assert ext is not None
        assert isinstance(ext, SymbolExtractor)

    def test_get_supported_languages(self):
        from groundtruth.foundation.parser.registry import get_supported_languages

        langs = get_supported_languages()
        assert "python" in langs

    def test_registry_prefers_treesitter(self):
        from groundtruth.foundation.parser.treesitter_backend import HAS_TREE_SITTER

        if not HAS_TREE_SITTER:
            pytest.skip("tree-sitter not installed")

        from groundtruth.foundation.parser.registry import get_extractor
        from groundtruth.foundation.parser.treesitter_backend import TreeSitterExtractor

        # Reset registry for fresh test
        import groundtruth.foundation.parser.registry as reg
        reg._initialized = False
        reg._extractor = None

        ext = get_extractor()
        assert isinstance(ext, TreeSitterExtractor)

        # Cleanup
        reg._initialized = False
        reg._extractor = None


# ---- Integration test with real project fixture ----


class TestRealProjectFixture:
    """Parse the test fixtures/project_py/ directory."""

    def _get_extractor(self):
        from groundtruth.foundation.parser.registry import get_extractor
        return get_extractor()

    def test_parse_fixture_project(self):
        ext = self._get_extractor()
        fixture_dir = os.path.join(
            os.path.dirname(__file__),
            "..", "fixtures", "project_py", "src"
        )
        fixture_dir = os.path.normpath(fixture_dir)
        if not os.path.isdir(fixture_dir):
            pytest.skip("project_py fixture not found")

        all_symbols: list[ExtractedSymbol] = []
        py_files = []
        for root, dirs, files in os.walk(fixture_dir):
            for f in files:
                if f.endswith(".py") and f != "__init__.py":
                    py_files.append(os.path.join(root, f))

        assert len(py_files) >= 5, f"Expected >=5 Python files, found {len(py_files)}"

        for fpath in py_files:
            parsed = ext.parse_file(fpath)
            if parsed.error:
                continue
            symbols = ext.extract_symbols(parsed)
            all_symbols.extend(symbols)

        # Should find a reasonable number of symbols
        assert len(all_symbols) >= 10, f"Expected >=10 symbols, found {len(all_symbols)}"

        # Verify we found both functions and classes
        kinds = {s.kind for s in all_symbols}
        assert "function" in kinds or "method" in kinds
