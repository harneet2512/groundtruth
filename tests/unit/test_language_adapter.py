"""Tests for language adapters and positive-evidence AST validation."""

from __future__ import annotations

import math
import time

import pytest

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok
from groundtruth.validators.ast_validator import AstValidator, AstValidationError
from groundtruth.validators.language_adapter import (
    GoAdapter,
    PythonAdapter,
    TypeScriptAdapter,
    get_adapter,
)


# ---------------------------------------------------------------------------
# PythonAdapter tests
# ---------------------------------------------------------------------------


class TestPythonAdapterParseImports:
    def test_from_import(self) -> None:
        adapter = PythonAdapter()
        imports = adapter.parse_imports("from os.path import join\n")
        assert len(imports) == 1
        assert imports[0].module == "os.path"
        assert imports[0].name == "join"
        assert imports[0].is_from is True

    def test_bare_import(self) -> None:
        adapter = PythonAdapter()
        imports = adapter.parse_imports("import json\n")
        assert len(imports) == 1
        assert imports[0].name == "json"
        assert imports[0].is_from is False

    def test_multiple_imports(self) -> None:
        adapter = PythonAdapter()
        code = "from os import path, getcwd\nimport sys\n"
        imports = adapter.parse_imports(code)
        assert len(imports) == 3

    def test_relative_import(self) -> None:
        adapter = PythonAdapter()
        imports = adapter.parse_imports("from .utils import helper\n")
        assert len(imports) == 1
        assert imports[0].is_relative is True

    def test_syntax_error_returns_empty(self) -> None:
        adapter = PythonAdapter()
        imports = adapter.parse_imports("def broken(:\n")
        assert imports == []


class TestPythonAdapterParseCalls:
    def test_simple_call(self) -> None:
        adapter = PythonAdapter()
        calls = adapter.parse_calls("result = foo(1, 2)\n")
        assert len(calls) >= 1
        foo_calls = [c for c in calls if c.function_name == "foo"]
        assert len(foo_calls) == 1
        assert foo_calls[0].arg_count == 2

    def test_method_call(self) -> None:
        adapter = PythonAdapter()
        calls = adapter.parse_calls("obj.method(a, b, c)\n")
        method_calls = [c for c in calls if c.function_name == "method"]
        assert len(method_calls) == 1
        assert method_calls[0].is_method_call is True
        assert method_calls[0].receiver == "obj"

    def test_keyword_args(self) -> None:
        adapter = PythonAdapter()
        calls = adapter.parse_calls("foo(a, b=1, c=2)\n")
        foo_calls = [c for c in calls if c.function_name == "foo"]
        assert len(foo_calls) == 1
        assert foo_calls[0].arg_count == 3


class TestPythonAdapterResolveArity:
    def test_simple_function(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity("(a: int, b: str) -> None", False)
        assert min_r == 2
        assert max_a == 2

    def test_method_subtracts_self(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity(
            "(self, a: int, b: str) -> None", True
        )
        assert min_r == 2
        assert max_a == 2

    def test_method_subtracts_cls(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity(
            "(cls, name: str) -> Foo", True
        )
        assert min_r == 1
        assert max_a == 1

    def test_variadic_args(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity(
            "(a: int, *args, **kwargs) -> None", False
        )
        assert min_r == 1
        assert max_a == math.inf

    def test_default_params(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity(
            "(a: int, b: str = 'x') -> None", False
        )
        assert min_r == 1
        assert max_a == 2

    def test_no_params(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity("() -> None", False)
        assert min_r == 0
        assert max_a == 0

    def test_unparseable_signature(self) -> None:
        adapter = PythonAdapter()
        min_r, max_a = adapter.resolve_effective_arity("some random text", False)
        assert max_a == math.inf  # treats as unparseable → permissive


class TestPythonAdapterDynamicExports:
    def test_detects_all(self) -> None:
        adapter = PythonAdapter()
        assert adapter.has_dynamic_exports("__all__ = ['foo', 'bar']")

    def test_detects_star_import(self) -> None:
        adapter = PythonAdapter()
        assert adapter.has_dynamic_exports("from .utils import *")

    def test_detects_getattr(self) -> None:
        adapter = PythonAdapter()
        assert adapter.has_dynamic_exports("def __getattr__(name):\n    pass")

    def test_no_dynamic_exports(self) -> None:
        adapter = PythonAdapter()
        assert not adapter.has_dynamic_exports("from os import path\nx = 1\n")


# ---------------------------------------------------------------------------
# Stub adapter tests
# ---------------------------------------------------------------------------


class TestStubAdapters:
    def test_typescript_returns_permissive(self) -> None:
        adapter = TypeScriptAdapter()
        assert adapter.parse_imports("import { foo } from './bar'") == []
        assert adapter.parse_calls("foo(1, 2)") == []
        min_r, max_a = adapter.resolve_effective_arity("(a: number) => void", False)
        assert max_a == math.inf

    def test_go_returns_permissive(self) -> None:
        adapter = GoAdapter()
        assert adapter.parse_imports('import "fmt"') == []
        min_r, max_a = adapter.resolve_effective_arity("func(a int) error", False)
        assert max_a == math.inf


class TestGetAdapter:
    def test_python(self) -> None:
        adapter = get_adapter("python")
        assert isinstance(adapter, PythonAdapter)

    def test_typescript(self) -> None:
        adapter = get_adapter("typescript")
        assert isinstance(adapter, TypeScriptAdapter)

    def test_javascript(self) -> None:
        adapter = get_adapter("javascript")
        assert isinstance(adapter, TypeScriptAdapter)

    def test_go(self) -> None:
        adapter = get_adapter("go")
        assert isinstance(adapter, GoAdapter)

    def test_unknown_returns_none(self) -> None:
        assert get_adapter("rust") is None
        assert get_adapter("java") is None
        assert get_adapter("haskell") is None


# ---------------------------------------------------------------------------
# AST validator: positive evidence tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> SymbolStore:
    """Create an in-memory store with test symbols."""
    s = SymbolStore(":memory:")
    s.initialize()
    now = int(time.time())

    # Populate with symbols in known files
    r = s.insert_symbol(
        name="get_user",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(user_id: int) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r, Ok)

    r = s.insert_symbol(
        name="get_user_by_email",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=12,
        end_line=20,
        is_exported=True,
        signature="(email: str) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r, Ok)

    # Add more symbols to meet coverage threshold
    for i in range(5):
        s.insert_symbol(
            name=f"helper_{i}",
            kind="function",
            language="python",
            file_path="src/users/queries.py",
            line_number=30 + i,
            end_line=35 + i,
            is_exported=True,
            signature=f"(x: int) -> int",
            params=None,
            return_type="int",
            documentation=None,
            last_indexed_at=now,
        )

    r = s.insert_symbol(
        name="hashPassword",
        kind="function",
        language="python",
        file_path="src/utils/crypto.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(password: str) -> str",
        params=None,
        return_type="str",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(r, Ok)

    return s


class TestAstValidatorPositiveEvidence:
    """Tests that the AST validator only emits positive-evidence findings."""

    def test_unknown_module_stays_silent(self, store: SymbolStore) -> None:
        """Unknown module (not in index) → SILENT."""
        code = "from unknown_module import SomeClass\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_bare_import_stays_silent(self, store: SymbolStore) -> None:
        """Bare import (import M) → always SILENT."""
        code = "import numpy\nimport unknown_pkg\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_stdlib_import_passes(self, store: SymbolStore) -> None:
        """Stdlib imports are always skipped."""
        code = "from os.path import join\nimport json\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_wrong_module_path_detected(self, store: SymbolStore) -> None:
        """Symbol exists at different path → wrong_module_path (positive evidence)."""
        # hashPassword exists in src/utils/crypto.py, not in src/auth
        # But we need the "from" module to have files in the index + symbol exists elsewhere
        # The module "auth" is not in the index, but hashPassword exists in crypto
        # However, with default-allow, unknown module → SILENT
        # We need a scenario where the module IS in the index but doesn't have the symbol
        # Let's test with a module that has files: src/users/queries.py
        code = "from users.queries import hashPassword\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        # hashPassword exists in crypto.py, not queries.py → positive evidence
        wrong_path = [e for e in result.value if e.error_type == "wrong_module_path"]
        assert len(wrong_path) == 1
        assert wrong_path[0].evidence_type == "positive_contradiction"

    def test_no_invented_symbol_error_type(self, store: SymbolStore) -> None:
        """The 'invented_symbol' error type no longer exists."""
        code = "from users.queries import totally_fake_function\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        error_types = {e.error_type for e in result.value}
        assert "invented_symbol" not in error_types

    def test_no_missing_package_error_type(self, store: SymbolStore) -> None:
        """The 'missing_package' error type no longer exists in AST validator."""
        code = "import nonexistent_package\nfrom nonexistent import foo\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        error_types = {e.error_type for e in result.value}
        assert "missing_package" not in error_types

    def test_unsupported_language_returns_empty(self, store: SymbolStore) -> None:
        """Unknown language → empty findings."""
        code = "fn main() { println!(\"hello\"); }"
        validator = AstValidator(store)
        result = validator.validate(code, "main.rs", "rust")
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_typescript_stays_silent(self, store: SymbolStore) -> None:
        """TypeScript with stub adapter → empty findings (silent)."""
        code = "import { foo } from './bar'\nfoo(1, 2, 3)\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.ts", "typescript")
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_go_stays_silent(self, store: SymbolStore) -> None:
        """Go with stub adapter → empty findings (silent)."""
        code = 'package main\nimport "fmt"\nfunc main() { fmt.Println("hi") }\n'
        validator = AstValidator(store)
        result = validator.validate(code, "main.go", "go")
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_evidence_type_field_present(self, store: SymbolStore) -> None:
        """All errors have evidence_type field."""
        code = "from users.queries import hashPassword\n"
        validator = AstValidator(store)
        result = validator.validate(code, "src/app.py", "python")
        assert isinstance(result, Ok)
        for err in result.value:
            assert hasattr(err, "evidence_type")
            assert err.evidence_type in (
                "positive_contradiction",
                "close_typo",
                "arity_mismatch",
            )
