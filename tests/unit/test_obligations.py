"""Tests for the obligation engine.

Covers all 4 obligation kinds: constructor_symmetry, override_contract,
caller_contract, shared_state. Includes positive, negative, and edge cases.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from groundtruth.index.graph import ImportGraph, Reference
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok
from groundtruth.validators.obligations import ObligationEngine


# ---------------------------------------------------------------------------
# Helpers — build SymbolRecord without repeating 14 fields every time
# ---------------------------------------------------------------------------

_NEXT_ID = 0


def _sym(
    name: str,
    kind: str = "function",
    file_path: str = "src/models.py",
    line: int | None = 1,
    end_line: int | None = None,
    **kw: object,
) -> SymbolRecord:
    global _NEXT_ID
    _NEXT_ID += 1
    return SymbolRecord(
        id=kw.get("id", _NEXT_ID),  # type: ignore[arg-type]
        name=name,
        kind=kind,
        language="python",
        file_path=file_path,
        line_number=line,
        end_line=end_line if end_line is not None else (line + 20 if line is not None else None),
        is_exported=True,
        signature=kw.get("signature"),  # type: ignore[arg-type]
        params=None,
        return_type=None,
        documentation=None,
        usage_count=0,
        last_indexed_at=0,
    )


def _make_engine(
    *,
    resolve: SymbolRecord | None = None,
    symbols_in_range: list[SymbolRecord] | None = None,
    symbols_in_file: list[SymbolRecord] | None = None,
    attributes: list[dict] | None = None,
    subclasses: list[SymbolRecord] | None = None,
    callers: list[Reference] | None = None,
    symbol_by_id: dict[int, SymbolRecord] | None = None,
) -> ObligationEngine:
    """Build an ObligationEngine with a fully mocked store and graph."""
    store = MagicMock(spec=SymbolStore)
    graph = MagicMock(spec=ImportGraph)

    store.resolve_symbol.return_value = Ok(resolve) if resolve else Ok(None)

    store.get_symbols_in_line_range.return_value = Ok(symbols_in_range or [])
    store.get_symbols_in_file.return_value = Ok(symbols_in_file or [])
    store.get_attributes_for_symbol.return_value = Ok(attributes or [])
    store.get_subclasses.return_value = Ok(subclasses or [])

    if symbol_by_id:
        store.get_symbol_by_id.side_effect = lambda mid: Ok(symbol_by_id.get(mid))
    else:
        store.get_symbol_by_id.return_value = Ok(None)

    if callers is not None:
        graph.find_callers.return_value = Ok(callers)
    else:
        graph.find_callers.return_value = Ok([])

    return ObligationEngine(store, graph)


# ===========================================================================
# constructor_symmetry
# ===========================================================================


class TestConstructorSymmetry:
    """__init__ sets attrs; structural methods must reference all of them."""

    def test_fires_when_repr_misses_attr(self) -> None:
        """__init__ sets {x, y, z}, __repr__ uses {x, y} → obligation on __repr__."""
        class_sym = _sym("Point", kind="class", id=100, line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=101, file_path="src/models.py", line=2, end_line=5)
        repr_sym = _sym("__repr__", kind="method", id=102, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 100, "name": "x", "method_ids": [101, 102]},
            {"id": 2, "symbol_id": 100, "name": "y", "method_ids": [101, 102]},
            {"id": 3, "symbol_id": 100, "name": "z", "method_ids": [101]},  # __repr__ misses z
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, repr_sym],
            attributes=attrs,
        )
        result = engine.infer("Point")

        assert len(result) == 1
        ob = result[0]
        assert ob.kind == "constructor_symmetry"
        assert ob.target == "Point.__repr__"
        assert ob.source == "Point.__init__"
        assert ob.confidence == 0.85
        assert "z" in ob.reason

    def test_fires_when_eq_misses_multiple_attrs(self) -> None:
        """__init__ sets {a, b, c, d}, __eq__ uses {a} → fires, lists missing {b, c, d}."""
        class_sym = _sym("Record", kind="class", id=200, line=1, end_line=40)
        init_sym = _sym("__init__", kind="method", id=201, file_path="src/models.py", line=2, end_line=5)
        eq_sym = _sym("__eq__", kind="method", id=202, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 200, "name": "a", "method_ids": [201, 202]},
            {"id": 2, "symbol_id": 200, "name": "b", "method_ids": [201]},
            {"id": 3, "symbol_id": 200, "name": "c", "method_ids": [201]},
            {"id": 4, "symbol_id": 200, "name": "d", "method_ids": [201]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, eq_sym],
            attributes=attrs,
        )
        result = engine.infer("Record")

        assert len(result) == 1
        assert result[0].kind == "constructor_symmetry"
        assert "b" in result[0].reason
        assert "c" in result[0].reason
        assert "d" in result[0].reason

    def test_fires_on_multiple_structural_methods(self) -> None:
        """__init__ sets {x, y}, both __eq__ and __hash__ miss y → 2 obligations."""
        class_sym = _sym("Pair", kind="class", id=300, line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=301, file_path="src/models.py", line=2, end_line=5)
        eq_sym = _sym("__eq__", kind="method", id=302, file_path="src/models.py", line=6, end_line=10)
        hash_sym = _sym("__hash__", kind="method", id=303, file_path="src/models.py", line=11, end_line=15)

        attrs = [
            {"id": 1, "symbol_id": 300, "name": "x", "method_ids": [301, 302, 303]},
            {"id": 2, "symbol_id": 300, "name": "y", "method_ids": [301]},  # both miss y
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, eq_sym, hash_sym],
            attributes=attrs,
        )
        result = engine.infer("Pair")

        assert len(result) == 2
        targets = {o.target for o in result}
        assert "Pair.__eq__" in targets
        assert "Pair.__hash__" in targets

    # --- Negative tests (must NOT fire) ---

    def test_no_fire_when_all_attrs_covered(self) -> None:
        """__init__ sets {x, y}, __eq__ uses {x, y} → no obligation."""
        class_sym = _sym("FullPoint", kind="class", id=400, line=1, end_line=20)
        init_sym = _sym("__init__", kind="method", id=401, file_path="src/models.py", line=2, end_line=5)
        eq_sym = _sym("__eq__", kind="method", id=402, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 400, "name": "x", "method_ids": [401, 402]},
            {"id": 2, "symbol_id": 400, "name": "y", "method_ids": [401, 402]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, eq_sym],
            attributes=attrs,
        )
        result = engine.infer("FullPoint")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        assert sym_obligations == []

    def test_no_fire_when_repr_intentionally_partial(self) -> None:
        """__repr__ intentionally shows only 'name' for a User(name, email, password_hash).

        This is the false-positive guard: __repr__ that omits sensitive/internal
        fields is deliberate design, not a bug. The engine fires here because it
        cannot distinguish intentional omission from accidental — this test
        documents the known precision limit.

        The obligation engine WILL fire (method_attrs is non-empty but not a
        superset of init_attrs). If in the future we add an opt-out mechanism,
        this test should be updated to assert no fire.
        """
        class_sym = _sym("User", kind="class", id=500, line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=501, file_path="src/models.py", line=2, end_line=5)
        repr_sym = _sym("__repr__", kind="method", id=502, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 500, "name": "name", "method_ids": [501, 502]},
            {"id": 2, "symbol_id": 500, "name": "email", "method_ids": [501]},
            {"id": 3, "symbol_id": 500, "name": "password_hash", "method_ids": [501]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, repr_sym],
            attributes=attrs,
        )
        result = engine.infer("User")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]

        # KNOWN PRECISION LIMIT: engine fires because it sees partial attr coverage.
        # This documents the false-positive. The obligation IS emitted.
        assert len(sym_obligations) == 1
        assert "email" in sym_obligations[0].reason or "password_hash" in sym_obligations[0].reason

    def test_no_fire_when_structural_method_uses_zero_attrs(self) -> None:
        """__repr__ returns a static string (no attrs at all) → no obligation.

        The engine only fires when method_attrs is non-empty but doesn't cover
        all init_attrs. A method that touches zero attrs is not an asymmetry.
        """
        class_sym = _sym("Singleton", kind="class", id=600, line=1, end_line=20)
        init_sym = _sym("__init__", kind="method", id=601, file_path="src/models.py", line=2, end_line=5)
        repr_sym = _sym("__repr__", kind="method", id=602, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 600, "name": "instance", "method_ids": [601]},
            # __repr__ (id=602) is NOT in any method_ids → touches zero attrs
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, repr_sym],
            attributes=attrs,
        )
        result = engine.infer("Singleton")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        assert sym_obligations == []

    def test_no_fire_when_no_structural_methods(self) -> None:
        """Class has __init__ and custom methods but no __eq__/__repr__/etc."""
        class_sym = _sym("Service", kind="class", id=700, line=1, end_line=20)
        init_sym = _sym("__init__", kind="method", id=701, file_path="src/models.py", line=2, end_line=5)
        run_sym = _sym("run", kind="method", id=702, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 700, "name": "config", "method_ids": [701]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, run_sym],
            attributes=attrs,
        )
        result = engine.infer("Service")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        assert sym_obligations == []

    # --- Edge cases ---

    def test_no_attrs_returns_empty(self) -> None:
        """Class with no attributes → no constructor symmetry obligations."""
        class_sym = _sym("Empty", kind="class", id=800, line=1, end_line=10)

        engine = _make_engine(resolve=class_sym, symbols_in_range=[], attributes=[])
        result = engine.infer("Empty")
        assert [o for o in result if o.kind == "constructor_symmetry"] == []

    def test_init_not_in_method_ids_falls_back_to_all_attrs(self) -> None:
        """When __init__ is not recorded in method_ids, fallback treats all attrs as init attrs."""
        class_sym = _sym("Legacy", kind="class", id=900, line=1, end_line=30)
        # No __init__ method in the range
        repr_sym = _sym("__repr__", kind="method", id=902, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 900, "name": "x", "method_ids": [902]},
            {"id": 2, "symbol_id": 900, "name": "y", "method_ids": []},  # not in __repr__
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[repr_sym],  # no __init__
            attributes=attrs,
        )
        result = engine.infer("Legacy")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        # Fallback: all attrs are init_attrs = {x, y}. __repr__ has {x}. Missing {y}.
        assert len(sym_obligations) == 1
        assert "y" in sym_obligations[0].reason

    # --- False-positive guards ---

    def test_no_fire_nonstructural_method_skipped(self) -> None:
        """A method named 'calculate' (not in _STRUCTURAL_METHODS) uses partial attrs.

        The engine must NOT fire because non-structural methods are skipped at
        line 159. This guards against false positives on custom business methods
        that intentionally use a subset of attributes.
        """
        class_sym = _sym("Stats", kind="class", id=950, line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=951, file_path="src/models.py", line=2, end_line=5)
        calc_sym = _sym("calculate", kind="method", id=952, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 950, "name": "total", "method_ids": [951, 952]},
            {"id": 2, "symbol_id": 950, "name": "count", "method_ids": [951]},  # calculate misses count
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, calc_sym],
            attributes=attrs,
        )
        result = engine.infer("Stats")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        assert sym_obligations == []

    def test_no_fire_structural_method_covers_superset(self) -> None:
        """__str__ references all init attrs plus additional non-init attrs.

        init_attrs.issubset(method_attrs) is True, so no obligation is emitted.
        Guards against false positives when a method over-covers.
        """
        class_sym = _sym("Detail", kind="class", id=960, line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=961, file_path="src/models.py", line=2, end_line=5)
        str_sym = _sym("__str__", kind="method", id=962, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 960, "name": "x", "method_ids": [961, 962]},
            {"id": 2, "symbol_id": 960, "name": "y", "method_ids": [961, 962]},
            # extra attr used by __str__ but NOT by __init__
            {"id": 3, "symbol_id": 960, "name": "cached_label", "method_ids": [962]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, str_sym],
            attributes=attrs,
        )
        result = engine.infer("Detail")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        # init_attrs = {x, y}, method_attrs for __str__ = {x, y, cached_label}
        # {x, y}.issubset({x, y, cached_label}) → True → no fire
        assert sym_obligations == []

    # --- Error branch coverage ---

    def test_attrs_result_err_returns_empty(self) -> None:
        """get_attributes_for_symbol returns Err → no constructor_symmetry obligations."""
        class_sym = _sym("Broken", kind="class", id=970, line=1, end_line=20)

        engine = _make_engine(resolve=class_sym, symbols_in_range=[])
        engine.store.get_attributes_for_symbol.return_value = Err(
            GroundTruthError(code="db_error", message="table missing")
        )
        result = engine.infer("Broken")
        assert [o for o in result if o.kind == "constructor_symmetry"] == []

    def test_method_ids_none_coerced_to_empty(self) -> None:
        """Attr dict has method_ids=None → or [] fallback at lines 144/165."""
        class_sym = _sym("NullIds", kind="class", id=980, line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=981, file_path="src/models.py", line=2, end_line=5)
        eq_sym = _sym("__eq__", kind="method", id=982, file_path="src/models.py", line=6, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 980, "name": "x", "method_ids": None},
            {"id": 2, "symbol_id": 980, "name": "y", "method_ids": None},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, eq_sym],
            attributes=attrs,
        )
        result = engine.infer("NullIds")
        sym_obligations = [o for o in result if o.kind == "constructor_symmetry"]
        # method_ids=None → or [] → no __init__ found → fallback: all attrs are init_attrs
        # __eq__ also has method_ids=None → or [] → method_attrs is empty → no fire
        # (engine only fires when method_attrs is non-empty)
        assert sym_obligations == []


# ===========================================================================
# override_contract
# ===========================================================================


class TestOverrideContract:
    """Base class method changes → subclass overrides are obligated."""

    def test_fires_when_subclass_overrides_method(self) -> None:
        """Base.process changes → Sub.process must update."""
        base = _sym("Base", kind="class", id=1000, file_path="src/base.py", line=1, end_line=20)
        base_process = _sym("process", kind="method", id=1001, file_path="src/base.py", line=5, end_line=10)

        sub = _sym("Sub", kind="class", id=1010, file_path="src/sub.py", line=1, end_line=20)
        sub_process = _sym("process", kind="method", id=1011, file_path="src/sub.py", line=5, end_line=10)

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[base_process],
            subclasses=[sub],
        )
        # Override _get_class_methods to return sub's methods when called for sub
        original_get = engine._get_class_methods
        def patched_get(cls: SymbolRecord) -> list[SymbolRecord]:
            if cls.id == sub.id:
                return [sub_process]
            return original_get(cls)
        engine._get_class_methods = patched_get  # type: ignore[assignment]

        result = engine.infer("Base")

        overrides = [o for o in result if o.kind == "override_contract"]
        assert len(overrides) == 1
        assert overrides[0].target == "Sub.process"
        assert overrides[0].source == "Base.process"
        assert overrides[0].confidence == 0.9
        assert overrides[0].target_file == "src/sub.py"

    def test_fires_for_multiple_subclasses(self) -> None:
        """Two subclasses both override the same method → 2 obligations."""
        base = _sym("Handler", kind="class", id=1100, file_path="src/handler.py", line=1, end_line=20)
        base_handle = _sym("handle", kind="method", id=1101, file_path="src/handler.py", line=5, end_line=10)

        sub_a = _sym("JsonHandler", kind="class", id=1110, file_path="src/json_handler.py", line=1, end_line=20)
        sub_a_handle = _sym("handle", kind="method", id=1111, file_path="src/json_handler.py", line=5, end_line=10)

        sub_b = _sym("XmlHandler", kind="class", id=1120, file_path="src/xml_handler.py", line=1, end_line=20)
        sub_b_handle = _sym("handle", kind="method", id=1121, file_path="src/xml_handler.py", line=5, end_line=10)

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[base_handle],
            subclasses=[sub_a, sub_b],
        )
        original_get = engine._get_class_methods
        def patched_get(cls: SymbolRecord) -> list[SymbolRecord]:
            if cls.id == sub_a.id:
                return [sub_a_handle]
            if cls.id == sub_b.id:
                return [sub_b_handle]
            return original_get(cls)
        engine._get_class_methods = patched_get  # type: ignore[assignment]

        result = engine.infer("Handler")
        overrides = [o for o in result if o.kind == "override_contract"]
        assert len(overrides) == 2
        targets = {o.target for o in overrides}
        assert "JsonHandler.handle" in targets
        assert "XmlHandler.handle" in targets

    # --- Negative tests ---

    def test_no_fire_when_subclass_has_no_override(self) -> None:
        """Subclass exists but does not override the method → no obligation."""
        base = _sym("Base", kind="class", id=1200, file_path="src/base.py", line=1, end_line=20)
        base_process = _sym("process", kind="method", id=1201, file_path="src/base.py", line=5, end_line=10)

        sub = _sym("Sub", kind="class", id=1210, file_path="src/sub.py", line=1, end_line=20)
        sub_other = _sym("other_method", kind="method", id=1211, file_path="src/sub.py", line=5, end_line=10)

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[base_process],
            subclasses=[sub],
        )
        original_get = engine._get_class_methods
        def patched_get(cls: SymbolRecord) -> list[SymbolRecord]:
            if cls.id == sub.id:
                return [sub_other]  # different method name
            return original_get(cls)
        engine._get_class_methods = patched_get  # type: ignore[assignment]

        result = engine.infer("Base")
        overrides = [o for o in result if o.kind == "override_contract"]
        assert overrides == []

    def test_no_fire_when_no_subclasses(self) -> None:
        """No subclasses at all → no override obligations."""
        base = _sym("Leaf", kind="class", id=1300, file_path="src/leaf.py", line=1, end_line=20)
        base_method = _sym("do_thing", kind="method", id=1301, file_path="src/leaf.py", line=5, end_line=10)

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[base_method],
            subclasses=[],
        )
        result = engine.infer("Leaf")
        overrides = [o for o in result if o.kind == "override_contract"]
        assert overrides == []

    # --- Edge case ---

    def test_no_fire_when_base_method_not_found(self) -> None:
        """get_subclasses returns results but the base class has no matching method."""
        base = _sym("Abstract", kind="class", id=1400, file_path="src/abs.py", line=1, end_line=20)
        # No methods in base range
        sub = _sym("Concrete", kind="class", id=1410, file_path="src/concrete.py", line=1, end_line=20)

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[],  # no methods
            subclasses=[sub],
        )
        result = engine.infer("Abstract")
        overrides = [o for o in result if o.kind == "override_contract"]
        assert overrides == []

    def test_no_fire_when_get_subclasses_returns_err(self) -> None:
        """store.get_subclasses returns Err → no override obligations."""
        base = _sym("ErrBase", kind="class", id=1500, file_path="src/base.py", line=1, end_line=20)
        base_method = _sym("run", kind="method", id=1501, file_path="src/base.py", line=5, end_line=10)

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[base_method],
        )
        engine.store.get_subclasses.return_value = Err(
            GroundTruthError(code="db_error", message="query failed")
        )
        result = engine.infer("ErrBase")
        overrides = [o for o in result if o.kind == "override_contract"]
        assert overrides == []

    def test_override_via_method_infer_with_enclosing_class(self) -> None:
        """infer() on a method → _find_enclosing_class → override_contract checked.

        Tests the full routing path (lines 59-61) without monkey-patching
        _get_class_methods. Uses symbols_in_file for enclosing class lookup and
        symbols_in_range for method listing within the class.
        """
        class_sym = _sym("Parent", kind="class", id=1600, file_path="src/parent.py", line=1, end_line=30)
        method_sym = _sym("action", kind="method", id=1601, file_path="src/parent.py", line=5, end_line=10)

        sub = _sym("Child", kind="class", id=1610, file_path="src/child.py", line=1, end_line=20)
        sub_action = _sym("action", kind="method", id=1611, file_path="src/child.py", line=5, end_line=10)

        engine = _make_engine(
            resolve=method_sym,
            # symbols_in_file is used by _find_enclosing_class
            symbols_in_file=[class_sym, method_sym],
            # symbols_in_range is used by _get_class_methods for both parent and child
            symbols_in_range=[method_sym],
            subclasses=[sub],
            callers=[],
        )
        # _get_class_methods is called for both Parent (to find base method)
        # and Child (to find override). We need it to return different results.
        original_get = engine._get_class_methods
        def patched_get(cls: SymbolRecord) -> list[SymbolRecord]:
            if cls.id == class_sym.id:
                return [method_sym]
            if cls.id == sub.id:
                return [sub_action]
            return original_get(cls)
        engine._get_class_methods = patched_get  # type: ignore[assignment]

        result = engine.infer("action")
        overrides = [o for o in result if o.kind == "override_contract"]
        assert len(overrides) == 1
        assert overrides[0].target == "Child.action"


# ===========================================================================
# caller_contract
# ===========================================================================


class TestCallerContract:
    """Function params change → all callers are obligated to update."""

    def test_fires_for_each_caller(self) -> None:
        """Function with 3 callers → 3 obligations."""
        func = _sym("get_user", kind="function", id=2000, file_path="src/queries.py", line=10)

        callers = [
            Reference(file_path="src/routes.py", line=25, context="get_user(id)"),
            Reference(file_path="src/api.py", line=42, context="get_user(user_id)"),
            Reference(file_path="tests/test_queries.py", line=8, context="get_user(1)"),
        ]

        engine = _make_engine(resolve=func, callers=callers)
        result = engine.infer("get_user")

        caller_obs = [o for o in result if o.kind == "caller_contract"]
        assert len(caller_obs) == 3
        files = {o.target_file for o in caller_obs}
        assert files == {"src/routes.py", "src/api.py", "tests/test_queries.py"}
        assert all(o.confidence == 0.7 for o in caller_obs)
        assert all(o.source == "get_user" for o in caller_obs)

    def test_fires_for_method_callers(self) -> None:
        """Method with callers also triggers caller_contract."""
        method = _sym("save", kind="method", id=2100, file_path="src/models.py", line=15)

        callers = [Reference(file_path="src/service.py", line=30, context="obj.save()")]

        # Method needs an enclosing class for override checks, but the caller
        # contract fires regardless of class context.
        engine = _make_engine(resolve=method, callers=callers, symbols_in_file=[])
        result = engine.infer("save")

        caller_obs = [o for o in result if o.kind == "caller_contract"]
        assert len(caller_obs) == 1
        assert caller_obs[0].target_file == "src/service.py"

    # --- Negative tests ---

    def test_no_fire_when_zero_callers(self) -> None:
        """Function with no callers → no obligations."""
        func = _sym("unused_helper", kind="function", id=2200, file_path="src/utils.py", line=5)

        engine = _make_engine(resolve=func, callers=[])
        result = engine.infer("unused_helper")

        caller_obs = [o for o in result if o.kind == "caller_contract"]
        assert caller_obs == []

    def test_no_fire_when_graph_returns_error(self) -> None:
        """find_callers returns Err → graceful empty result, no crash."""
        func = _sym("broken", kind="function", id=2300, file_path="src/broken.py", line=1)

        engine = _make_engine(resolve=func)
        # Override to return Err
        engine.graph.find_callers.return_value = Err(
            GroundTruthError(code="db_error", message="connection lost")
        )
        result = engine.infer("broken")

        caller_obs = [o for o in result if o.kind == "caller_contract"]
        assert caller_obs == []

    # --- Edge case ---

    def test_callers_deduped_by_file_and_target(self) -> None:
        """Same file, same caller → only one obligation (dedup by target+file)."""
        func = _sym("parse", kind="function", id=2400, file_path="src/parser.py", line=1)

        # Two calls in the same file at different lines
        callers = [
            Reference(file_path="src/main.py", line=10, context="parse(a)"),
            Reference(file_path="src/main.py", line=20, context="parse(b)"),
        ]

        engine = _make_engine(resolve=func, callers=callers)
        result = engine.infer("parse")

        caller_obs = [o for o in result if o.kind == "caller_contract"]
        # Both have the same target_file but different target text ("call site in src/main.py")
        # so dedup key is (kind, target, target_file) — target is the same string
        # "call site in src/main.py" for both → only 1 after dedup
        assert len(caller_obs) == 1

    def test_caller_with_none_line(self) -> None:
        """Reference with line=None → obligation still created with target_line=None."""
        func = _sym("module_init", kind="function", id=2500, file_path="src/init.py", line=1)

        callers = [Reference(file_path="src/app.py", line=None, context="module_init()")]

        engine = _make_engine(resolve=func, callers=callers)
        result = engine.infer("module_init")

        caller_obs = [o for o in result if o.kind == "caller_contract"]
        assert len(caller_obs) == 1
        assert caller_obs[0].target_line is None
        assert caller_obs[0].target_file == "src/app.py"


# ===========================================================================
# shared_state
# ===========================================================================


class TestSharedState:
    """Methods sharing self.attr are semantically coupled."""

    def test_fires_for_methods_sharing_attr(self) -> None:
        """Two methods read/write self.count → both are obligated."""
        class_sym = _sym("Counter", kind="class", id=3000, file_path="src/counter.py", line=1, end_line=30)
        increment = _sym("increment", kind="method", id=3001, file_path="src/counter.py", line=5, end_line=10)
        decrement = _sym("decrement", kind="method", id=3002, file_path="src/counter.py", line=11, end_line=16)

        attrs = [
            {"id": 1, "symbol_id": 3000, "name": "count", "method_ids": [3001, 3002]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            attributes=attrs,
            symbol_by_id={3001: increment, 3002: decrement},
        )
        result = engine._shared_state(class_sym, "count")

        assert len(result) == 2
        assert all(o.kind == "shared_state" for o in result)
        assert all(o.confidence == 0.6 for o in result)
        targets = {o.target for o in result}
        assert "Counter.increment" in targets
        assert "Counter.decrement" in targets

    def test_fires_only_for_matching_attr(self) -> None:
        """Only methods touching the specified attr are returned."""
        class_sym = _sym("Widget", kind="class", id=3100, file_path="src/widget.py", line=1, end_line=30)
        render = _sym("render", kind="method", id=3101, file_path="src/widget.py", line=5, end_line=10)
        resize = _sym("resize", kind="method", id=3102, file_path="src/widget.py", line=11, end_line=16)

        attrs = [
            {"id": 1, "symbol_id": 3100, "name": "width", "method_ids": [3101, 3102]},
            {"id": 2, "symbol_id": 3100, "name": "color", "method_ids": [3101]},  # only render
        ]

        engine = _make_engine(
            resolve=class_sym,
            attributes=attrs,
            symbol_by_id={3101: render, 3102: resize},
        )

        width_result = engine._shared_state(class_sym, "width")
        assert len(width_result) == 2

        color_result = engine._shared_state(class_sym, "color")
        assert len(color_result) == 1
        assert color_result[0].target == "Widget.render"

    # --- Negative tests ---

    def test_no_fire_when_attr_not_found(self) -> None:
        """Querying a non-existent attr → empty."""
        class_sym = _sym("Box", kind="class", id=3200, file_path="src/box.py", line=1, end_line=20)

        engine = _make_engine(resolve=class_sym, attributes=[])
        result = engine._shared_state(class_sym, "nonexistent")
        assert result == []

    def test_no_fire_when_attr_has_no_methods(self) -> None:
        """Attr exists but method_ids is empty → no obligations."""
        class_sym = _sym("Config", kind="class", id=3300, file_path="src/config.py", line=1, end_line=20)

        attrs = [
            {"id": 1, "symbol_id": 3300, "name": "debug", "method_ids": []},
        ]

        engine = _make_engine(resolve=class_sym, attributes=attrs)
        result = engine._shared_state(class_sym, "debug")
        assert result == []

    def test_no_fire_when_store_returns_error(self) -> None:
        """get_attributes_for_symbol returns Err → graceful empty."""
        class_sym = _sym("Broken", kind="class", id=3400, file_path="src/broken.py", line=1, end_line=20)

        engine = _make_engine(resolve=class_sym)
        engine.store.get_attributes_for_symbol.return_value = Err(
            GroundTruthError(code="db_error", message="table missing")
        )
        result = engine._shared_state(class_sym, "x")
        assert result == []

    def test_method_ids_none_produces_no_obligations(self) -> None:
        """Attr has method_ids=None → or [] at line 251 → no iteration."""
        class_sym = _sym("NullState", kind="class", id=3500, file_path="src/null.py", line=1, end_line=20)

        attrs = [
            {"id": 1, "symbol_id": 3500, "name": "value", "method_ids": None},
        ]

        engine = _make_engine(resolve=class_sym, attributes=attrs)
        result = engine._shared_state(class_sym, "value")
        assert result == []

    def test_symbol_by_id_returns_none_skipped(self) -> None:
        """get_symbol_by_id returns Ok(None) → obligation not created."""
        class_sym = _sym("Ghost", kind="class", id=3600, file_path="src/ghost.py", line=1, end_line=20)

        attrs = [
            {"id": 1, "symbol_id": 3600, "name": "phantom", "method_ids": [9999]},
        ]

        engine = _make_engine(resolve=class_sym, attributes=attrs)
        # symbol_by_id not configured → returns Ok(None) by default
        result = engine._shared_state(class_sym, "phantom")
        assert result == []

    def test_shared_state_called_from_infer_for_class(self) -> None:
        """infer() on a class produces shared_state obligations for coupled methods."""
        class_sym = _sym("Coupled", kind="class", id=3700, file_path="src/coupled.py", line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=3701, file_path="src/coupled.py", line=2, end_line=5)
        read_sym = _sym("read", kind="method", id=3702, file_path="src/coupled.py", line=6, end_line=10)
        write_sym = _sym("write", kind="method", id=3703, file_path="src/coupled.py", line=11, end_line=16)

        attrs = [
            {"id": 1, "symbol_id": 3700, "name": "data", "method_ids": [3701, 3702, 3703]},
        ]

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[init_sym, read_sym, write_sym],
            attributes=attrs,
            symbol_by_id={3701: init_sym, 3702: read_sym, 3703: write_sym},
        )
        result = engine.infer("Coupled")
        shared = [o for o in result if o.kind == "shared_state"]
        assert len(shared) == 3, "all 3 methods sharing self.data should be obligated"
        targets = {o.target for o in shared}
        assert "Coupled.__init__" in targets
        assert "Coupled.read" in targets
        assert "Coupled.write" in targets

    def test_shared_state_called_from_infer_for_method(self) -> None:
        """infer() on a method produces shared_state for sibling methods sharing attrs."""
        class_sym = _sym("Coupled", kind="class", id=3800, file_path="src/coupled.py", line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=3801, file_path="src/coupled.py", line=2, end_line=5)
        read_sym = _sym("read", kind="method", id=3802, file_path="src/coupled.py", line=6, end_line=10)
        write_sym = _sym("write", kind="method", id=3803, file_path="src/coupled.py", line=11, end_line=16)

        attrs = [
            {"id": 1, "symbol_id": 3800, "name": "data", "method_ids": [3801, 3802, 3803]},
        ]

        engine = _make_engine(
            resolve=read_sym,
            symbols_in_range=[init_sym, read_sym, write_sym],
            symbols_in_file=[class_sym, init_sym, read_sym, write_sym],
            attributes=attrs,
            symbol_by_id={3801: init_sym, 3802: read_sym, 3803: write_sym},
        )
        result = engine.infer("read")
        shared = [o for o in result if o.kind == "shared_state"]
        # read itself should be excluded; __init__ and write should be obligated
        assert len(shared) == 2
        targets = {o.target for o in shared}
        assert "Coupled.__init__" in targets
        assert "Coupled.write" in targets
        assert "Coupled.read" not in targets


# ===========================================================================
# infer — routing and dedup
# ===========================================================================


class TestInferRouting:
    """Test that infer() routes correctly based on symbol kind."""

    def test_class_routes_to_constructor_and_override(self) -> None:
        """A class symbol triggers constructor_symmetry and override checks."""
        class_sym = _sym("Model", kind="class", id=4000, line=1, end_line=20)

        engine = _make_engine(
            resolve=class_sym,
            symbols_in_range=[],
            attributes=[],
            subclasses=[],
        )
        # Should not crash, should return empty (no attrs, no subclasses)
        result = engine.infer("Model")
        assert result == []

    def test_function_routes_to_caller_contract(self) -> None:
        """A function symbol triggers caller_contract."""
        func = _sym("helper", kind="function", id=4100, line=5)
        caller = Reference(file_path="src/main.py", line=10, context="helper()")

        engine = _make_engine(resolve=func, callers=[caller])
        result = engine.infer("helper")

        assert len(result) == 1
        assert result[0].kind == "caller_contract"

    def test_method_routes_to_caller_and_override(self) -> None:
        """A method triggers caller_contract + override check via enclosing class."""
        method = _sym("do_work", kind="method", id=4200, file_path="src/worker.py", line=10)
        caller = Reference(file_path="src/runner.py", line=20, context="do_work()")

        engine = _make_engine(resolve=method, callers=[caller], symbols_in_file=[])
        result = engine.infer("do_work")

        assert len(result) >= 1
        assert any(o.kind == "caller_contract" for o in result)

    def test_unresolved_symbol_returns_empty(self) -> None:
        """Symbol that doesn't exist in the store → empty obligations."""
        engine = _make_engine(resolve=None)
        result = engine.infer("nonexistent_function")
        assert result == []

    def test_dedup_removes_duplicates(self) -> None:
        """Duplicate obligations (same kind, target, file) are deduplicated."""
        func = _sym("dup_target", kind="function", id=4300, line=5)
        # Two callers at different lines in the same file produce the same
        # dedup key: ("caller_contract", "call site in src/a.py", "src/a.py")
        callers = [
            Reference(file_path="src/a.py", line=10, context="dup_target()"),
            Reference(file_path="src/a.py", line=20, context="dup_target()"),
        ]

        engine = _make_engine(resolve=func, callers=callers)
        result = engine.infer("dup_target")

        assert len(result) == 1

    def test_cap_at_10_obligations(self) -> None:
        """Even with many callers, results are capped at 10."""
        func = _sym("popular", kind="function", id=4400, line=5)
        callers = [
            Reference(file_path=f"src/file_{i}.py", line=i, context="popular()")
            for i in range(15)
        ]

        engine = _make_engine(resolve=func, callers=callers)
        result = engine.infer("popular")

        assert len(result) <= 10

    def test_resolve_err_returns_empty(self) -> None:
        """store.resolve_symbol returns Err → _resolve returns None → empty."""
        engine = _make_engine()
        engine.store.resolve_symbol.return_value = Err(
            GroundTruthError(code="resolve_error", message="ambiguous symbol")
        )
        result = engine.infer("ambiguous")
        assert result == []

    def test_variable_kind_routes_to_caller_only(self) -> None:
        """Symbol with kind='variable' → else branch, only caller_contract."""
        var = _sym("CONFIG", kind="variable", id=4500, file_path="src/settings.py", line=5)
        caller = Reference(file_path="src/app.py", line=10, context="CONFIG")

        engine = _make_engine(resolve=var, callers=[caller])
        result = engine.infer("CONFIG")

        assert len(result) == 1
        assert result[0].kind == "caller_contract"
        # Verify no constructor_symmetry or override_contract
        assert not any(o.kind in ("constructor_symmetry", "override_contract") for o in result)

    def test_capital_class_kind_routes_correctly(self) -> None:
        """Symbol with kind='Class' (capital C) → same routing as 'class'."""
        cls = _sym("Widget", kind="Class", id=4600, line=1, end_line=20)

        engine = _make_engine(
            resolve=cls,
            symbols_in_range=[],
            attributes=[],
            subclasses=[],
        )
        # Should not crash — routes through constructor_symmetry + override_contract
        result = engine.infer("Widget")
        assert isinstance(result, list)

    def test_confidence_sort_order(self) -> None:
        """Mixed obligation types sorted by confidence: override (0.9) before constructor (0.85)."""
        base = _sym("Ordered", kind="class", id=4700, file_path="src/ordered.py", line=1, end_line=30)
        init_sym = _sym("__init__", kind="method", id=4701, file_path="src/ordered.py", line=2, end_line=5)
        eq_sym = _sym("__eq__", kind="method", id=4702, file_path="src/ordered.py", line=6, end_line=10)
        process_sym = _sym("process", kind="method", id=4703, file_path="src/ordered.py", line=11, end_line=15)

        sub = _sym("SubOrdered", kind="class", id=4710, file_path="src/sub_ordered.py", line=1, end_line=20)
        sub_process = _sym("process", kind="method", id=4711, file_path="src/sub_ordered.py", line=5, end_line=10)

        attrs = [
            {"id": 1, "symbol_id": 4700, "name": "x", "method_ids": [4701, 4702]},
            {"id": 2, "symbol_id": 4700, "name": "y", "method_ids": [4701]},  # __eq__ misses y
        ]

        engine = _make_engine(
            resolve=base,
            symbols_in_range=[init_sym, eq_sym, process_sym],
            attributes=attrs,
            subclasses=[sub],
        )
        original_get = engine._get_class_methods
        def patched_get(cls: SymbolRecord) -> list[SymbolRecord]:
            if cls.id == sub.id:
                return [sub_process]
            return original_get(cls)
        engine._get_class_methods = patched_get  # type: ignore[assignment]

        result = engine.infer("Ordered")
        assert len(result) >= 2
        # override_contract (0.9) should come before constructor_symmetry (0.85)
        kinds = [o.kind for o in result]
        override_idx = kinds.index("override_contract")
        constructor_idx = kinds.index("constructor_symmetry")
        assert override_idx < constructor_idx, (
            f"override_contract (conf=0.9) should sort before constructor_symmetry (conf=0.85), "
            f"got override at {override_idx}, constructor at {constructor_idx}"
        )


# ===========================================================================
# infer_from_patch — diff parsing
# ===========================================================================


class TestInferFromPatch:
    """Test diff parsing and obligation inference from patches."""

    def test_parses_function_def_from_diff(self) -> None:
        """Extracts function name from +def line in a unified diff."""
        diff = """\
--- a/src/queries.py
+++ b/src/queries.py
@@ -10,6 +10,8 @@
+    def get_user(self, user_id):
+        return self.db.find(user_id)
"""
        func = _sym("get_user", kind="function", id=5000, file_path="src/queries.py", line=10)
        caller = Reference(file_path="src/routes.py", line=5, context="get_user(1)")

        engine = _make_engine(resolve=func, callers=[caller])
        result = engine.infer_from_patch(diff)

        assert len(result) >= 1
        assert any(o.source == "get_user" for o in result)

    def test_parses_class_def_from_diff(self) -> None:
        """Extracts class name from +class line."""
        diff = """\
--- a/src/models.py
+++ b/src/models.py
@@ -1,3 +1,5 @@
+class UserProfile:
+    pass
"""
        cls = _sym("UserProfile", kind="class", id=5100, file_path="src/models.py", line=1, end_line=5)

        engine = _make_engine(
            resolve=cls,
            symbols_in_range=[],
            attributes=[],
            subclasses=[],
        )
        result = engine.infer_from_patch(diff)
        # Class with no attrs/subclasses → empty, but no crash
        assert isinstance(result, list)

    def test_parses_async_def(self) -> None:
        """Handles 'async def' correctly."""
        diff = """\
--- a/src/api.py
+++ b/src/api.py
@@ -5,3 +5,5 @@
+    async def fetch_data(self):
+        pass
"""
        func = _sym("fetch_data", kind="function", id=5200, file_path="src/api.py", line=5)

        engine = _make_engine(resolve=func, callers=[])
        result = engine.infer_from_patch(diff)
        # No callers → empty, but parsing succeeded
        assert isinstance(result, list)

    def test_ignores_removed_lines(self) -> None:
        """Lines starting with - (removals) are not parsed as symbols."""
        diff = """\
--- a/src/old.py
+++ b/src/old.py
@@ -1,5 +1,3 @@
-def old_function():
-    pass
+def new_function():
+    pass
"""
        func = _sym("new_function", kind="function", id=5300, file_path="src/old.py", line=1)

        engine = _make_engine(resolve=func, callers=[])
        result = engine.infer_from_patch(diff)
        # Should only see new_function, not old_function
        assert isinstance(result, list)

    def test_empty_diff_returns_empty(self) -> None:
        """Empty string → no obligations."""
        engine = _make_engine()
        result = engine.infer_from_patch("")
        assert result == []

    def test_multiple_files_in_diff(self) -> None:
        """Diff spanning two files extracts symbols from both."""
        diff = """\
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,5 @@
+def func_a():
+    pass
--- a/src/b.py
+++ b/src/b.py
@@ -1,3 +1,5 @@
+def func_b():
+    pass
"""
        engine = _make_engine()
        # _parse_changed_symbols should find both
        parsed = engine._parse_changed_symbols(diff)
        names = {name for name, _ in parsed}
        assert "func_a" in names
        assert "func_b" in names
        files = {fp for _, fp in parsed}
        assert "src/a.py" in files
        assert "src/b.py" in files

    def test_plus_plus_plus_without_b_prefix(self) -> None:
        """+++ header without b/ prefix → line 280-281 strips '+++ ' and strips whitespace."""
        diff = """\
--- a/src/foo.py
+++ src/foo.py
@@ -1,3 +1,5 @@
+def extracted():
+    pass
"""
        engine = _make_engine()
        parsed = engine._parse_changed_symbols(diff)
        assert len(parsed) == 1
        assert parsed[0] == ("extracted", "src/foo.py")

    def test_context_lines_ignored(self) -> None:
        """Unchanged context lines containing 'def' are NOT parsed as changed symbols."""
        diff = """\
--- a/src/ctx.py
+++ b/src/ctx.py
@@ -1,5 +1,6 @@
 def existing_function():
     pass
+def new_function():
+    pass
"""
        engine = _make_engine()
        parsed = engine._parse_changed_symbols(diff)
        names = {name for name, _ in parsed}
        assert "new_function" in names
        assert "existing_function" not in names

    def test_decorated_function_extracted(self) -> None:
        """Decorator line is not a symbol; only the def line produces a symbol."""
        diff = """\
--- a/src/deco.py
+++ b/src/deco.py
@@ -1,3 +1,5 @@
+@some_decorator
+def decorated_func():
+    pass
"""
        engine = _make_engine()
        parsed = engine._parse_changed_symbols(diff)
        names = {name for name, _ in parsed}
        assert "decorated_func" in names
        assert "some_decorator" not in names

    def test_plus_header_not_parsed_as_symbol(self) -> None:
        """+++ b/src/def.py should not be treated as a symbol even though it contains 'def'."""
        diff = """\
--- a/src/def.py
+++ b/src/def.py
@@ -1,3 +1,5 @@
+def real_symbol():
+    pass
"""
        engine = _make_engine()
        parsed = engine._parse_changed_symbols(diff)
        # Only real_symbol, not anything from the +++ header line
        assert len(parsed) == 1
        assert parsed[0][0] == "real_symbol"


# ===========================================================================
# _find_enclosing_class — dedicated tests
# ===========================================================================


class TestFindEnclosingClass:
    """Test _find_enclosing_class directly — previously only implicit coverage."""

    def test_finds_class_by_line_range(self) -> None:
        """Method at line 10, class spans 5-25 → returns the class."""
        class_sym = _sym("Container", kind="class", id=6000, file_path="src/container.py", line=5, end_line=25)
        method_sym = _sym("inner", kind="method", id=6001, file_path="src/container.py", line=10, end_line=15)

        engine = _make_engine(
            symbols_in_file=[class_sym, method_sym],
        )
        result = engine._find_enclosing_class(method_sym)
        assert result is not None
        assert result.name == "Container"
        assert result.id == 6000

    def test_returns_none_when_method_line_is_none(self) -> None:
        """Method with line_number=None → cannot determine containment → None."""
        class_sym = _sym("Outer", kind="class", id=6100, file_path="src/outer.py", line=5, end_line=25)
        method_sym = _sym("orphan", kind="method", id=6101, file_path="src/outer.py", line=None, end_line=None)

        engine = _make_engine(
            symbols_in_file=[class_sym, method_sym],
        )
        result = engine._find_enclosing_class(method_sym)
        assert result is None

    def test_returns_none_when_store_returns_err(self) -> None:
        """get_symbols_in_file returns Err → None."""
        method_sym = _sym("lost", kind="method", id=6200, file_path="src/lost.py", line=10)

        engine = _make_engine()
        engine.store.get_symbols_in_file.return_value = Err(
            GroundTruthError(code="db_error", message="file not indexed")
        )
        result = engine._find_enclosing_class(method_sym)
        assert result is None


# ===========================================================================
# _get_class_methods — dedicated tests
# ===========================================================================


class TestGetClassMethods:
    """Test _get_class_methods directly."""

    def test_returns_empty_when_class_has_none_line_numbers(self) -> None:
        """Class with line_number=None → early return []."""
        class_sym = _sym("NoLines", kind="class", id=7000, file_path="src/nolines.py", line=None, end_line=None)

        engine = _make_engine()
        result = engine._get_class_methods(class_sym)
        assert result == []
