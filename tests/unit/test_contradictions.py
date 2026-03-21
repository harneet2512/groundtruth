"""Tests for the contradiction detector — positive structural evidence only.

For each contradiction kind, tests:
1. Positive: contradiction IS present with clear evidence -> fires
2. Negative: similar code but no contradiction -> does NOT fire
3. Edge case: uncertain situation -> does NOT fire (silence wins)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok
from groundtruth.validators.contradictions import ContradictionDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NEXT_ID = 0


def _sym(
    name: str,
    kind: str = "function",
    file_path: str = "src/models.py",
    line: int | None = 1,
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
        end_line=end_line if end_line is not None else (line + 20 if line is not None else None),
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
# Override violation tests
# ===========================================================================


class TestOverrideViolation:
    """Tests for check_override_violation."""

    def test_fires_when_override_has_wrong_param_count(self) -> None:
        """Override with different required param count than base -> fires."""
        store = _make_store()

        base_class = _sym("Animal", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_method = _sym(
            "speak", kind="method", file_path="src/base.py",
            line=5, end_line=10, signature="(self, volume: int)",
        )

        store.find_symbol_by_name.side_effect = lambda name: {
            "Animal": Ok([base_class]),
        }.get(name, Ok([]))

        store.get_symbols_in_file.return_value = Ok([base_class, base_method])

        source = """\
class Dog(Animal):
    def speak(self):
        return "woof"
"""
        detector = ContradictionDetector(store)
        results = detector.check_override_violation(source, "src/dog.py")

        assert len(results) == 1
        c = results[0]
        assert c.kind == "override_violation"
        assert "Dog.speak" in c.message
        assert "Animal.speak" in c.message
        assert c.confidence > 0.0

    def test_silent_when_override_matches_base_params(self) -> None:
        """Override with matching param count -> silent."""
        store = _make_store()

        base_class = _sym("Animal", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_method = _sym(
            "speak", kind="method", file_path="src/base.py",
            line=5, end_line=10, signature="(self, volume: int)",
        )

        store.find_symbol_by_name.side_effect = lambda name: {
            "Animal": Ok([base_class]),
        }.get(name, Ok([]))

        store.get_symbols_in_file.return_value = Ok([base_class, base_method])

        source = """\
class Dog(Animal):
    def speak(self, volume: int):
        return "woof" * volume
"""
        detector = ContradictionDetector(store)
        results = detector.check_override_violation(source, "src/dog.py")
        assert len(results) == 0

    def test_silent_when_base_has_variadic(self) -> None:
        """Base method uses *args -> uncertain -> silent."""
        store = _make_store()

        base_class = _sym("Animal", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_method = _sym(
            "speak", kind="method", file_path="src/base.py",
            line=5, end_line=10, signature="(self, *args)",
        )

        store.find_symbol_by_name.side_effect = lambda name: {
            "Animal": Ok([base_class]),
        }.get(name, Ok([]))

        store.get_symbols_in_file.return_value = Ok([base_class, base_method])

        source = """\
class Dog(Animal):
    def speak(self, volume: int, pitch: int):
        pass
"""
        detector = ContradictionDetector(store)
        results = detector.check_override_violation(source, "src/dog.py")
        assert len(results) == 0

    def test_silent_when_base_not_in_store(self) -> None:
        """Base class not in store -> no evidence -> silent."""
        store = _make_store()
        store.find_symbol_by_name.return_value = Ok([])

        source = """\
class Dog(Animal):
    def speak(self):
        return "woof"
"""
        detector = ContradictionDetector(store)
        results = detector.check_override_violation(source, "src/dog.py")
        assert len(results) == 0

    def test_silent_for_dunder_methods(self) -> None:
        """Dunder methods have special calling conventions -> silent."""
        store = _make_store()

        base_class = _sym("Base", kind="class", file_path="src/base.py", line=1, end_line=30)
        base_init = _sym(
            "__init__", kind="method", file_path="src/base.py",
            line=5, end_line=10, signature="(self, x: int, y: int)",
        )

        store.find_symbol_by_name.side_effect = lambda name: {
            "Base": Ok([base_class]),
        }.get(name, Ok([]))
        store.get_symbols_in_file.return_value = Ok([base_class, base_init])

        source = """\
class Child(Base):
    def __init__(self):
        super().__init__(0, 0)
"""
        detector = ContradictionDetector(store)
        results = detector.check_override_violation(source, "src/child.py")
        assert len(results) == 0


# ===========================================================================
# Arity mismatch tests
# ===========================================================================


class TestArityMismatch:
    """Tests for check_arity_mismatch."""

    def test_fires_when_too_few_args(self) -> None:
        """Call with fewer args than required -> fires."""
        store = _make_store()

        func = _sym(
            "process", kind="function", file_path="src/utils.py",
            signature="(data: list, mode: str)",
        )
        store.find_symbol_by_name.side_effect = lambda name: {
            "process": Ok([func]),
        }.get(name, Ok([]))

        source = """\
result = process(my_data)
"""
        detector = ContradictionDetector(store)
        results = detector.check_arity_mismatch(source, "src/main.py")

        assert len(results) == 1
        c = results[0]
        assert c.kind == "arity_mismatch"
        assert "process" in c.message
        assert c.confidence > 0.0

    def test_silent_when_args_match(self) -> None:
        """Call with correct arg count -> silent."""
        store = _make_store()

        func = _sym(
            "process", kind="function", file_path="src/utils.py",
            signature="(data: list, mode: str)",
        )
        store.find_symbol_by_name.side_effect = lambda name: {
            "process": Ok([func]),
        }.get(name, Ok([]))

        source = """\
result = process(my_data, "fast")
"""
        detector = ContradictionDetector(store)
        results = detector.check_arity_mismatch(source, "src/main.py")
        assert len(results) == 0

    def test_silent_when_function_has_kwargs(self) -> None:
        """Function has **kwargs -> uncertain max arity -> silent."""
        store = _make_store()

        func = _sym(
            "process", kind="function", file_path="src/utils.py",
            signature="(data: list, **kwargs)",
        )
        store.find_symbol_by_name.side_effect = lambda name: {
            "process": Ok([func]),
        }.get(name, Ok([]))

        source = """\
result = process(my_data, extra=True, verbose=False)
"""
        detector = ContradictionDetector(store)
        results = detector.check_arity_mismatch(source, "src/main.py")
        assert len(results) == 0

    def test_silent_when_call_uses_star_args(self) -> None:
        """Call uses *args unpacking -> uncertain actual count -> silent."""
        store = _make_store()

        func = _sym(
            "process", kind="function", file_path="src/utils.py",
            signature="(a: int, b: int, c: int)",
        )
        store.find_symbol_by_name.side_effect = lambda name: {
            "process": Ok([func]),
        }.get(name, Ok([]))

        source = """\
args = [1, 2, 3]
result = process(*args)
"""
        detector = ContradictionDetector(store)
        results = detector.check_arity_mismatch(source, "src/main.py")
        assert len(results) == 0

    def test_silent_when_ambiguous_function(self) -> None:
        """Multiple functions with same name -> ambiguous -> silent."""
        store = _make_store()

        func1 = _sym("process", kind="function", file_path="src/a.py", signature="(x: int)")
        func2 = _sym("process", kind="function", file_path="src/b.py", signature="(x: int, y: int)")
        store.find_symbol_by_name.side_effect = lambda name: {
            "process": Ok([func1, func2]),
        }.get(name, Ok([]))

        source = """\
result = process()
"""
        detector = ContradictionDetector(store)
        results = detector.check_arity_mismatch(source, "src/main.py")
        assert len(results) == 0

    def test_silent_when_function_not_in_store(self) -> None:
        """Function not in store -> no evidence -> silent."""
        store = _make_store()
        store.find_symbol_by_name.return_value = Ok([])

        source = """\
result = unknown_function(1, 2, 3)
"""
        detector = ContradictionDetector(store)
        results = detector.check_arity_mismatch(source, "src/main.py")
        assert len(results) == 0


# ===========================================================================
# Import path moved tests
# ===========================================================================


class TestImportPathMoved:
    """Tests for check_import_path_moved."""

    def test_fires_when_symbol_at_different_path(self) -> None:
        """Symbol exists in store at a different path -> fires."""
        store = _make_store()

        sym = _sym("UserService", kind="class", file_path="src/services/user.py")
        store.find_symbol_by_name.side_effect = lambda name: {
            "UserService": Ok([sym]),
        }.get(name, Ok([]))

        source = """\
from models.user import UserService
"""
        detector = ContradictionDetector(store)
        results = detector.check_import_path_moved(source, "src/main.py")

        assert len(results) == 1
        c = results[0]
        assert c.kind == "import_path_moved"
        assert "UserService" in c.message
        assert "models.user" in c.message
        assert c.confidence > 0.0

    def test_silent_when_import_path_correct(self) -> None:
        """Symbol exists at the imported path -> no contradiction -> silent."""
        store = _make_store()

        sym = _sym("UserService", kind="class", file_path="src/services/user.py")
        store.find_symbol_by_name.side_effect = lambda name: {
            "UserService": Ok([sym]),
        }.get(name, Ok([]))

        source = """\
from services.user import UserService
"""
        detector = ContradictionDetector(store)
        results = detector.check_import_path_moved(source, "src/main.py")
        assert len(results) == 0

    def test_silent_when_symbol_unknown(self) -> None:
        """Symbol not in store at all -> might be external -> silent."""
        store = _make_store()
        store.find_symbol_by_name.return_value = Ok([])

        source = """\
from requests import Session
"""
        detector = ContradictionDetector(store)
        results = detector.check_import_path_moved(source, "src/main.py")
        assert len(results) == 0


# ===========================================================================
# Integration: check_file combines all checks
# ===========================================================================


class TestCheckFile:
    """Tests for check_file combining all checks."""

    def test_empty_file_returns_empty(self) -> None:
        """Empty source code -> no contradictions."""
        store = _make_store()
        detector = ContradictionDetector(store)
        results = detector.check_file("src/empty.py", "")
        assert results == []

    def test_syntax_error_returns_empty(self) -> None:
        """Unparseable source -> no contradictions (silent)."""
        store = _make_store()
        detector = ContradictionDetector(store)
        results = detector.check_file("src/broken.py", "def foo(:\n  pass")
        assert results == []

    def test_combines_multiple_check_results(self) -> None:
        """check_file returns results from all sub-checks."""
        store = _make_store()

        # Set up: arity mismatch for process, import moved for Helper
        func = _sym("process", kind="function", file_path="src/utils.py", signature="(x: int, y: int)")
        helper = _sym("Helper", kind="class", file_path="src/core/helper.py")

        store.find_symbol_by_name.side_effect = lambda name: {
            "process": Ok([func]),
            "Helper": Ok([helper]),
        }.get(name, Ok([]))

        source = """\
from old_module import Helper

result = process()
"""
        detector = ContradictionDetector(store)
        results = detector.check_file("src/main.py", source)

        kinds = {c.kind for c in results}
        assert "arity_mismatch" in kinds
        assert "import_path_moved" in kinds
