"""Tests for AST-based Python symbol and import extraction."""

from __future__ import annotations

import os
import tempfile

import pytest

from groundtruth.index.ast_parser import ASTImport, ASTSymbol, parse_python_file, parse_python_imports


def _write_tmp(code: str, suffix: str = ".py") -> str:
    """Write code to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8")
    f.write(code)
    f.close()
    return f.name


class TestParsePythonFile:
    def test_extracts_functions(self) -> None:
        path = _write_tmp("def greet(name: str) -> str:\n    '''Say hello.'''\n    return f'hi {name}'\n")
        try:
            symbols = parse_python_file(path)
            assert len(symbols) == 1
            s = symbols[0]
            assert s.name == "greet"
            assert s.kind == "function"
            assert s.line == 0
            assert s.signature == "(name: str) -> str"
            assert s.return_type == "str"
            assert s.is_exported is True
            assert s.documentation == "Say hello."
        finally:
            os.unlink(path)

    def test_extracts_async_functions(self) -> None:
        path = _write_tmp("async def fetch(url: str) -> bytes:\n    pass\n")
        try:
            symbols = parse_python_file(path)
            assert len(symbols) == 1
            assert symbols[0].name == "fetch"
            assert symbols[0].kind == "function"
            assert symbols[0].signature == "(url: str) -> bytes"
        finally:
            os.unlink(path)

    def test_extracts_classes_with_methods(self) -> None:
        code = (
            "class User:\n"
            "    '''A user.'''\n"
            "    def get_name(self) -> str:\n"
            "        return self.name\n"
            "    def set_name(self, name: str) -> None:\n"
            "        self.name = name\n"
        )
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            assert len(symbols) == 1
            cls = symbols[0]
            assert cls.name == "User"
            assert cls.kind == "class"
            assert cls.documentation == "A user."
            assert len(cls.children) == 2
            assert cls.children[0].name == "get_name"
            assert cls.children[0].kind == "method"
            assert cls.children[1].name == "set_name"
            assert cls.children[1].kind == "method"
        finally:
            os.unlink(path)

    def test_extracts_properties(self) -> None:
        code = (
            "class Foo:\n"
            "    @property\n"
            "    def bar(self) -> int:\n"
            "        return 42\n"
        )
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            cls = symbols[0]
            assert len(cls.children) == 1
            assert cls.children[0].name == "bar"
            assert cls.children[0].kind == "property"
        finally:
            os.unlink(path)

    def test_extracts_module_variables_uppercase(self) -> None:
        code = "MAX_RETRIES = 3\nDEFAULT_TIMEOUT = 30\nlocal_var = 'skip'\n"
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            names = [s.name for s in symbols]
            assert "MAX_RETRIES" in names
            assert "DEFAULT_TIMEOUT" in names
            assert "local_var" not in names
        finally:
            os.unlink(path)

    def test_extracts_annotated_variables(self) -> None:
        code = "config: dict[str, int] = {}\n"
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            assert len(symbols) == 1
            assert symbols[0].name == "config"
            assert symbols[0].kind == "variable"
            assert symbols[0].signature == "dict[str, int]"
        finally:
            os.unlink(path)

    def test_signature_with_defaults(self) -> None:
        code = "def connect(host: str, port: int = 8080, ssl: bool = True) -> None:\n    pass\n"
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            sig = symbols[0].signature
            assert sig is not None
            assert "host: str" in sig
            assert "port: int = ..." in sig
            assert "ssl: bool = ..." in sig
            assert "-> None" in sig
        finally:
            os.unlink(path)

    def test_signature_with_args_kwargs(self) -> None:
        code = "def variadic(*args: int, **kwargs: str) -> None:\n    pass\n"
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            sig = symbols[0].signature
            assert sig is not None
            assert "*args: int" in sig
            assert "**kwargs: str" in sig
        finally:
            os.unlink(path)

    def test_docstring_extraction(self) -> None:
        code = 'def documented():\n    """First line.\n\n    More details here.\n    """\n    pass\n'
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            assert symbols[0].documentation == "First line."
        finally:
            os.unlink(path)

    def test_private_not_exported(self) -> None:
        code = "def _helper():\n    pass\ndef __dunder():\n    pass\ndef public():\n    pass\n"
        path = _write_tmp(code)
        try:
            symbols = parse_python_file(path)
            by_name = {s.name: s for s in symbols}
            assert by_name["_helper"].is_exported is False
            assert by_name["__dunder"].is_exported is False
            assert by_name["public"].is_exported is True
        finally:
            os.unlink(path)

    def test_syntax_error_returns_empty(self) -> None:
        path = _write_tmp("def broken(\n")
        try:
            symbols = parse_python_file(path)
            assert symbols == []
        finally:
            os.unlink(path)

    def test_empty_file(self) -> None:
        path = _write_tmp("")
        try:
            symbols = parse_python_file(path)
            assert symbols == []
        finally:
            os.unlink(path)

    def test_real_fixture_file(self) -> None:
        fixture = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "project_py", "src", "users", "queries.py"
        )
        if not os.path.isfile(fixture):
            pytest.skip("Fixture file not found")
        symbols = parse_python_file(fixture)
        names = [s.name for s in symbols]
        assert "get_user_by_id" in names
        assert "create_user" in names
        assert "update_user" in names
        assert "delete_user" in names
        assert len(symbols) == 4
        # Check signature for get_user_by_id
        get_fn = [s for s in symbols if s.name == "get_user_by_id"][0]
        assert get_fn.signature is not None
        assert "user_id: int" in get_fn.signature
        assert "-> User" in get_fn.signature


class TestParsePythonImports:
    def test_parse_import(self) -> None:
        code = "import os\nimport json\n"
        path = _write_tmp(code)
        try:
            imports = parse_python_imports(path)
            assert len(imports) == 2
            assert imports[0].name == "os"
            assert imports[0].is_from is False
            assert imports[0].module == "os"
        finally:
            os.unlink(path)

    def test_parse_from_import(self) -> None:
        code = "from os.path import join, exists\n"
        path = _write_tmp(code)
        try:
            imports = parse_python_imports(path)
            assert len(imports) == 2
            names = [i.name for i in imports]
            assert "join" in names
            assert "exists" in names
            assert all(i.module == "os.path" for i in imports)
            assert all(i.is_from for i in imports)
        finally:
            os.unlink(path)

    def test_parse_imports_relative(self) -> None:
        code = "from ..utils import helper\nfrom . import config\n"
        path = _write_tmp(code)
        try:
            imports = parse_python_imports(path)
            assert len(imports) == 2
            rel_import = [i for i in imports if i.name == "helper"][0]
            assert rel_import.level == 2
            assert rel_import.module == "utils"
            dot_import = [i for i in imports if i.name == "config"][0]
            assert dot_import.level == 1
        finally:
            os.unlink(path)

    def test_parse_import_with_alias(self) -> None:
        code = "import numpy as np\nfrom collections import OrderedDict as OD\n"
        path = _write_tmp(code)
        try:
            imports = parse_python_imports(path)
            np_import = [i for i in imports if i.name == "numpy"][0]
            assert np_import.alias == "np"
            od_import = [i for i in imports if i.name == "OrderedDict"][0]
            assert od_import.alias == "OD"
        finally:
            os.unlink(path)

    def test_syntax_error_returns_empty(self) -> None:
        path = _write_tmp("from broken import (\n")
        try:
            imports = parse_python_imports(path)
            assert imports == []
        finally:
            os.unlink(path)

    def test_line_numbers_zero_indexed(self) -> None:
        code = "# comment\nimport os\nfrom sys import argv\n"
        path = _write_tmp(code)
        try:
            imports = parse_python_imports(path)
            assert imports[0].line == 1  # 0-indexed: line 2 in file
            assert imports[1].line == 2
        finally:
            os.unlink(path)
