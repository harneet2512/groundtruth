"""Contradiction negative fixture corpus — 20 cases that must NOT fire.

These test realistic code patterns where a naive detector might false-positive,
but our positive-evidence-only approach must stay silent.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Ok
from groundtruth.validators.contradictions import ContradictionDetector


_NEXT_ID = 1000


def _sym(
    name: str,
    kind: str = "function",
    file_path: str = "src/models.py",
    line: int = 1,
    end_line: int | None = None,
    signature: str | None = None,
) -> SymbolRecord:
    global _NEXT_ID
    _NEXT_ID += 1
    return SymbolRecord(
        id=_NEXT_ID,
        name=name,
        kind=kind,
        language="python",
        file_path=file_path,
        line_number=line,
        end_line=end_line or line + 20,
        is_exported=True,
        signature=signature,
        params=None,
        return_type=None,
        documentation=None,
        usage_count=0,
        last_indexed_at=0,
    )


def _make_store() -> MagicMock:
    store = MagicMock(spec=SymbolStore)
    store.find_symbol_by_name.return_value = Ok([])
    store.get_symbols_in_file.return_value = Ok([])
    return store


# ===========================================================================
# Override violation negatives (7 cases)
# ===========================================================================


class TestOverrideNegatives:
    """Patterns that should NOT trigger override_violation."""

    def test_compatible_override_with_defaults(self) -> None:
        """Override adds extra params with defaults — still compatible."""
        store = _make_store()
        base_cls = _sym("Base", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_method = _sym("run", kind="method", file_path="src/base.py", line=5, signature="(self, x: int)")
        store.find_symbol_by_name.side_effect = lambda n: {"Base": Ok([base_cls])}.get(n, Ok([]))
        store.get_symbols_in_file.return_value = Ok([base_cls, base_method])

        source = """
class Child(Base):
    def run(self, x: int, verbose: bool = False):
        pass
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []

    def test_override_with_star_args(self) -> None:
        """Override accepts *args — compatible with any base."""
        store = _make_store()
        base_cls = _sym("Base", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_method = _sym("run", kind="method", file_path="src/base.py", line=5, signature="(self, a, b, c)")
        store.find_symbol_by_name.side_effect = lambda n: {"Base": Ok([base_cls])}.get(n, Ok([]))
        store.get_symbols_in_file.return_value = Ok([base_cls, base_method])

        source = """
class Child(Base):
    def run(self, *args, **kwargs):
        pass
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []

    def test_no_base_class(self) -> None:
        """Class without explicit bases — no override possible."""
        store = _make_store()
        source = """
class Standalone:
    def process(self):
        pass
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []

    def test_base_method_not_in_store(self) -> None:
        """Base class exists but method not indexed — no evidence → silent."""
        store = _make_store()
        base_cls = _sym("Base", kind="class", file_path="src/base.py", line=1, end_line=30)
        store.find_symbol_by_name.side_effect = lambda n: {"Base": Ok([base_cls])}.get(n, Ok([]))
        store.get_symbols_in_file.return_value = Ok([base_cls])  # no methods

        source = """
class Child(Base):
    def mystery_method(self):
        pass
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []

    def test_mixin_class_not_in_store(self) -> None:
        """Mixin base not in store — external or dynamic — silent."""
        store = _make_store()
        source = """
class MyView(LoginRequiredMixin, View):
    def dispatch(self, request):
        pass
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []

    def test_dunder_init_override(self) -> None:
        """__init__ override — skipped because dunder methods have special conventions."""
        store = _make_store()
        base_cls = _sym("Widget", kind="class", file_path="src/ui.py", line=1, end_line=50)
        base_init = _sym("__init__", kind="method", file_path="src/ui.py", line=3, signature="(self, x, y, w, h)")
        store.find_symbol_by_name.side_effect = lambda n: {"Widget": Ok([base_cls])}.get(n, Ok([]))
        store.get_symbols_in_file.return_value = Ok([base_cls, base_init])

        source = """
class Button(Widget):
    def __init__(self, label):
        super().__init__(0, 0, 100, 30)
        self.label = label
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []

    def test_base_has_no_signature(self) -> None:
        """Base method indexed but signature is None — incomplete evidence → silent."""
        store = _make_store()
        base_cls = _sym("Base", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_method = _sym("run", kind="method", file_path="src/base.py", line=5, signature=None)
        store.find_symbol_by_name.side_effect = lambda n: {"Base": Ok([base_cls])}.get(n, Ok([]))
        store.get_symbols_in_file.return_value = Ok([base_cls, base_method])

        source = """
class Child(Base):
    def run(self):
        pass
"""
        detector = ContradictionDetector(store)
        assert detector.check_override_violation(source, "test.py") == []


# ===========================================================================
# Arity mismatch negatives (7 cases)
# ===========================================================================


class TestArityNegatives:
    """Patterns that should NOT trigger arity_mismatch."""

    def test_correct_arg_count(self) -> None:
        """Exact match — no contradiction."""
        store = _make_store()
        func = _sym("compute", kind="function", signature="(a: int, b: int)")
        store.find_symbol_by_name.side_effect = lambda n: {"compute": Ok([func])}.get(n, Ok([]))

        source = "result = compute(1, 2)\n"
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []

    def test_call_with_kwargs(self) -> None:
        """Call uses keyword arguments — could satisfy params → silent."""
        store = _make_store()
        func = _sym("compute", kind="function", signature="(a: int, b: int)")
        store.find_symbol_by_name.side_effect = lambda n: {"compute": Ok([func])}.get(n, Ok([]))

        source = "result = compute(a=1, b=2)\n"
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []

    def test_function_has_args_variadic(self) -> None:
        """Function accepts *args — can't know max → silent."""
        store = _make_store()
        func = _sym("log", kind="function", signature="(*messages)")
        store.find_symbol_by_name.side_effect = lambda n: {"log": Ok([func])}.get(n, Ok([]))

        source = "log()\n"
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []

    def test_call_uses_double_star(self) -> None:
        """Call uses **kwargs unpacking — can't determine actual count → silent."""
        store = _make_store()
        func = _sym("setup", kind="function", signature="(host: str, port: int)")
        store.find_symbol_by_name.side_effect = lambda n: {"setup": Ok([func])}.get(n, Ok([]))

        source = """
config = {"host": "localhost", "port": 8080}
setup(**config)
"""
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []

    def test_method_call_on_object(self) -> None:
        """obj.method() call — 'method' might be from a different class → ambiguous."""
        store = _make_store()
        # Two methods named 'save' in different classes
        m1 = _sym("save", kind="method", file_path="src/a.py", signature="(self, data)")
        m2 = _sym("save", kind="method", file_path="src/b.py", signature="(self)")
        store.find_symbol_by_name.side_effect = lambda n: {"save": Ok([m1, m2])}.get(n, Ok([]))

        source = "obj.save()\n"
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []

    def test_class_kind_not_function(self) -> None:
        """Symbol is a class, not a function — calling it is __init__ → silent."""
        store = _make_store()
        cls = _sym("Config", kind="class", signature="(self, path: str)")
        store.find_symbol_by_name.side_effect = lambda n: {"Config": Ok([cls])}.get(n, Ok([]))

        source = "c = Config()\n"
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []

    def test_function_with_all_defaults(self) -> None:
        """All params have defaults — zero required → calling with zero args is fine."""
        store = _make_store()
        func = _sym("connect", kind="function", signature="(host: str = 'localhost', port: int = 5432)")
        store.find_symbol_by_name.side_effect = lambda n: {"connect": Ok([func])}.get(n, Ok([]))

        source = "db = connect()\n"
        detector = ContradictionDetector(store)
        assert detector.check_arity_mismatch(source, "test.py") == []


# ===========================================================================
# Import path moved negatives (6 cases)
# ===========================================================================


class TestImportPathNegatives:
    """Patterns that should NOT trigger import_path_moved."""

    def test_correct_import_path(self) -> None:
        """Import path matches where symbol actually lives."""
        store = _make_store()
        sym = _sym("Router", kind="class", file_path="src/web/router.py")
        store.find_symbol_by_name.side_effect = lambda n: {"Router": Ok([sym])}.get(n, Ok([]))

        source = "from web.router import Router\n"
        detector = ContradictionDetector(store)
        assert detector.check_import_path_moved(source, "test.py") == []

    def test_external_package(self) -> None:
        """Symbol not in store — could be external package → silent."""
        store = _make_store()
        source = "from flask import Flask\n"
        detector = ContradictionDetector(store)
        assert detector.check_import_path_moved(source, "test.py") == []

    def test_stdlib_import(self) -> None:
        """Standard library import not in store → silent."""
        store = _make_store()
        source = "from pathlib import Path\n"
        detector = ContradictionDetector(store)
        assert detector.check_import_path_moved(source, "test.py") == []

    def test_relative_import(self) -> None:
        """Relative import (module is None) → silent."""
        store = _make_store()
        source = "from . import helpers\n"
        detector = ContradictionDetector(store)
        assert detector.check_import_path_moved(source, "test.py") == []

    def test_init_package_import(self) -> None:
        """Import from package __init__ — file_path ends with __init__.py."""
        store = _make_store()
        sym = _sym("Client", kind="class", file_path="src/api/__init__.py")
        store.find_symbol_by_name.side_effect = lambda n: {"Client": Ok([sym])}.get(n, Ok([]))

        source = "from api import Client\n"
        detector = ContradictionDetector(store)
        assert detector.check_import_path_moved(source, "test.py") == []

    def test_store_error(self) -> None:
        """Store returns error — no evidence → silent."""
        store = _make_store()
        from groundtruth.utils.result import Err, GroundTruthError
        store.find_symbol_by_name.return_value = Err(GroundTruthError(code="db_error", message="oops"))

        source = "from models import User\n"
        detector = ContradictionDetector(store)
        assert detector.check_import_path_moved(source, "test.py") == []


# ===========================================================================
# Summary: 20 negative cases total
#   - 7 override_violation negatives
#   - 7 arity_mismatch negatives
#   - 6 import_path_moved negatives
# All must produce zero contradictions (zero false positives).
# ===========================================================================
