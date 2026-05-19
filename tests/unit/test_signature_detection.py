"""Tests for diff-aware signature arity mismatch detection."""
from groundtruth.hooks.post_edit import (
    _signature_param_count,
    _signature_has_varargs,
    _signature_default_count,
    _extract_call_arity,
    _check_arity_mismatch,
)


class TestSignatureParamCount:
    def test_simple(self):
        assert _signature_param_count("def foo(a, b, c):") == 3

    def test_with_self(self):
        assert _signature_param_count("def foo(self, a, b):") == 2

    def test_with_cls(self):
        assert _signature_param_count("def foo(cls, a):") == 1

    def test_no_params(self):
        assert _signature_param_count("def foo():") == 0

    def test_self_only(self):
        assert _signature_param_count("def foo(self):") == 0

    def test_with_defaults(self):
        assert _signature_param_count("def foo(a, b=1, c=2):") == 3

    def test_with_type_hints(self):
        assert _signature_param_count("def foo(a: int, b: str) -> bool:") == 2

    def test_empty(self):
        assert _signature_param_count("") is None

    def test_no_parens(self):
        assert _signature_param_count("foo") is None


class TestSignatureHasVarargs:
    def test_star_args(self):
        assert _signature_has_varargs("def foo(*args):") is True

    def test_double_star(self):
        assert _signature_has_varargs("def foo(**kwargs):") is True

    def test_no_varargs(self):
        assert _signature_has_varargs("def foo(a, b):") is False

    def test_empty(self):
        assert _signature_has_varargs("") is False


class TestSignatureDefaultCount:
    def test_no_defaults(self):
        assert _signature_default_count("def foo(a, b):") == 0

    def test_with_defaults(self):
        assert _signature_default_count("def foo(a, b=1, c=2):") == 2

    def test_self_default_excluded(self):
        assert _signature_default_count("def foo(self, a, b=1):") == 1

    def test_empty(self):
        assert _signature_default_count("") == 0


class TestExtractCallArity:
    def test_simple(self):
        assert _extract_call_arity("result = foo(a, b)", "foo") == 2

    def test_no_args(self):
        assert _extract_call_arity("foo()", "foo") == 0

    def test_one_arg(self):
        assert _extract_call_arity("foo(x)", "foo") == 1

    def test_nested_call(self):
        assert _extract_call_arity("foo(bar(1), 2)", "foo") == 2

    def test_not_found(self):
        assert _extract_call_arity("bar(1, 2)", "foo") is None

    def test_empty(self):
        assert _extract_call_arity("", "foo") is None


class TestCheckArityMismatch:
    def test_no_mismatch(self):
        callers = [{"file": "a.py", "line": "10", "code": "foo(1, 2)", "resolution_method": "import"}]
        result = _check_arity_mismatch("def foo(a, b):", "foo", callers, [])
        assert result == ""

    def test_mismatch_high_confidence(self):
        callers = [{"file": "a.py", "line": "10", "code": "foo(1)", "resolution_method": "import"}]
        result = _check_arity_mismatch("def foo(a, b, c):", "foo", callers, [])
        assert "[GT_CONTRACT high]" in result
        assert "a.py:10" in result

    def test_mismatch_medium_confidence(self):
        callers = [{"file": "a.py", "line": "10", "code": "foo(1)", "resolution_method": "name_match"}]
        result = _check_arity_mismatch("def foo(a, b, c):", "foo", callers, [])
        assert "[GT_CONTRACT medium]" in result

    def test_defaults_cover_gap(self):
        # foo(a, b=1, c=2) — caller passes 1 arg, but min_required = 1 (b and c have defaults)
        callers = [{"file": "a.py", "line": "10", "code": "foo(x)", "resolution_method": "import"}]
        result = _check_arity_mismatch("def foo(a, b=1, c=2):", "foo", callers, [])
        assert result == ""

    def test_varargs_suppresses(self):
        callers = [{"file": "a.py", "line": "10", "code": "foo(1)", "resolution_method": "import"}]
        result = _check_arity_mismatch("def foo(*args):", "foo", callers, [])
        assert result == ""

    def test_kwargs_suppresses(self):
        callers = [{"file": "a.py", "line": "10", "code": "foo(1)", "resolution_method": "import"}]
        result = _check_arity_mismatch("def foo(**kwargs):", "foo", callers, [])
        assert result == ""

    def test_caller_already_edited(self):
        callers = [{"file": "a.py", "line": "10", "code": "foo(1)", "resolution_method": "import"}]
        result = _check_arity_mismatch("def foo(a, b, c):", "foo", callers, ["a.py"])
        assert result == ""

    def test_self_excluded(self):
        # Method: def foo(self, a, b) — 2 real params. Caller passes 2 → no mismatch.
        callers = [{"file": "a.py", "line": "10", "code": "obj.foo(1, 2)", "resolution_method": "import"}]
        # _extract_call_arity for "obj.foo(1, 2)" with func_name="foo" → finds "foo(1, 2)" → 2
        result = _check_arity_mismatch("def foo(self, a, b):", "foo", callers, [])
        assert result == ""

    def test_no_callers(self):
        result = _check_arity_mismatch("def foo(a, b, c):", "foo", [], [])
        assert result == ""
