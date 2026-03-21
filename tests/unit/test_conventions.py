"""Tests for convention detectors."""

from __future__ import annotations

from groundtruth.analysis.conventions import (
    Convention,
    detect_all,
    detect_error_types,
    detect_guard_clauses,
    detect_return_shapes,
)


# ---------------------------------------------------------------------------
# Guard clause tests
# ---------------------------------------------------------------------------

CLASS_WITH_GUARDS = '''\
class Validator:
    def check_name(self, name):
        if not name:
            raise ValueError("name required")
        return name.strip()

    def check_age(self, age):
        if age < 0:
            raise ValueError("age must be positive")
        return age

    def check_email(self, email):
        if "@" not in email:
            raise ValueError("invalid email")
        return email.lower()

    def check_phone(self, phone):
        if not phone.isdigit():
            raise ValueError("digits only")
        return phone

    def format_output(self, data):
        return str(data)
'''


def test_guard_clause_fires_when_majority() -> None:
    """4/5 public methods have guards -> 80% -> fires."""
    results = detect_guard_clauses(CLASS_WITH_GUARDS, class_name="Validator")
    assert len(results) == 1
    conv = results[0]
    assert conv.kind == "guard_clause"
    assert conv.frequency == 0.8
    assert conv.scope == "Validator"
    assert len(conv.examples) == 4


CLASS_FEW_GUARDS = '''\
class Processor:
    def step_one(self, x):
        return x + 1

    def step_two(self, x):
        return x * 2

    def step_three(self, x):
        return x - 1

    def step_four(self, x):
        return x / 2

    def step_five(self, x):
        if not x:
            raise ValueError("empty")
        return x
'''


def test_guard_clause_does_not_fire_when_minority() -> None:
    """1/5 methods have guards -> 20% -> does not fire."""
    results = detect_guard_clauses(CLASS_FEW_GUARDS, class_name="Processor")
    assert results == []


MODULE_LEVEL_GUARDS = '''\
def validate_input(data):
    if not data:
        raise ValueError("no data")
    return data

def process(data):
    if len(data) == 0:
        raise RuntimeError("empty")
    return data

def run(data):
    if data is None:
        raise TypeError("None")
    return data
'''


def test_guard_clause_module_level() -> None:
    """Module-level functions with guards -> fires."""
    results = detect_guard_clauses(MODULE_LEVEL_GUARDS)
    assert len(results) == 1
    assert results[0].scope == "<module>"
    assert results[0].frequency == 1.0


def test_guard_clause_with_docstrings() -> None:
    """Guard clause detection should skip past docstrings."""
    source = '''\
class Service:
    def create(self, name):
        """Create a new item."""
        if not name:
            raise ValueError("name required")
        return {"name": name}

    def update(self, id, name):
        """Update an item."""
        if not id:
            raise ValueError("id required")
        return {"id": id, "name": name}

    def delete(self, id):
        """Delete an item."""
        if id is None:
            raise ValueError("id required")
        return True
'''
    results = detect_guard_clauses(source, class_name="Service")
    assert len(results) == 1
    assert results[0].frequency == 1.0


# ---------------------------------------------------------------------------
# Error type tests
# ---------------------------------------------------------------------------

MODULE_ALL_VALUE_ERROR = '''\
def foo():
    raise ValueError("bad")

def bar():
    raise ValueError("wrong")

def baz():
    raise ValueError("nope")
'''


def test_error_type_fires_when_dominant() -> None:
    """All raises use ValueError -> fires."""
    results = detect_error_types(MODULE_ALL_VALUE_ERROR)
    assert len(results) == 1
    conv = results[0]
    assert conv.kind == "error_type"
    assert conv.frequency == 1.0
    assert "ValueError" in conv.pattern
    assert conv.examples == ["ValueError"]


MODULE_MIXED_ERRORS = '''\
def foo():
    raise ValueError("bad")

def bar():
    raise TypeError("wrong")

def baz():
    raise RuntimeError("nope")
'''


def test_error_type_does_not_fire_when_mixed() -> None:
    """Three different exception types -> does not fire."""
    results = detect_error_types(MODULE_MIXED_ERRORS)
    assert results == []


def test_error_type_no_raises() -> None:
    """No raise statements -> does not fire."""
    source = '''\
def foo():
    return 1

def bar():
    return 2
'''
    results = detect_error_types(source)
    assert results == []


def test_error_type_scoped_to_class() -> None:
    """Error type detection scoped to a specific class."""
    source = '''\
class Repo:
    def get(self, id):
        raise KeyError("not found")

    def delete(self, id):
        raise KeyError("not found")

    def update(self, id, data):
        raise KeyError("not found")

def unrelated():
    raise ValueError("something else")
'''
    results = detect_error_types(source, scope="Repo")
    assert len(results) == 1
    assert results[0].scope == "Repo"
    assert "KeyError" in results[0].pattern


# ---------------------------------------------------------------------------
# Return shape tests
# ---------------------------------------------------------------------------

CLASS_RETURNS_DICT = '''\
class Builder:
    def build_user(self, name):
        return {"name": name}

    def build_item(self, title):
        return {"title": title}

    def build_order(self, item_id):
        return {"item_id": item_id}
'''


def test_return_shape_fires_when_dominant() -> None:
    """All methods return dict -> fires."""
    results = detect_return_shapes(CLASS_RETURNS_DICT, class_name="Builder")
    assert len(results) == 1
    conv = results[0]
    assert conv.kind == "return_shape"
    assert conv.frequency == 1.0
    assert "dict" in conv.pattern


CLASS_MIXED_RETURNS = '''\
class Mixed:
    def as_dict(self):
        return {"a": 1}

    def as_list(self):
        return [1, 2, 3]

    def as_tuple(self):
        return (1, 2)

    def as_str(self):
        return "hello"
'''


def test_return_shape_does_not_fire_when_mixed() -> None:
    """Mixed return types -> does not fire."""
    results = detect_return_shapes(CLASS_MIXED_RETURNS, class_name="Mixed")
    assert results == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_class_no_conventions() -> None:
    """Empty class -> no conventions detected."""
    source = '''\
class Empty:
    pass
'''
    results = detect_all(source, scope="Empty")
    assert results == []


def test_private_methods_excluded() -> None:
    """Private methods should not count toward frequency."""
    source = '''\
class Service:
    def _internal_check(self):
        return [1]

    def _helper(self):
        return [2]

    def handle(self, data):
        if not data:
            raise ValueError("no data")
        return {"result": data}
'''
    # Only 1 public method (handle), which has a guard -> 100%
    guards = detect_guard_clauses(source, class_name="Service")
    assert len(guards) == 1
    assert guards[0].frequency == 1.0

    # Only 1 public method returns dict -> 100%
    shapes = detect_return_shapes(source, class_name="Service")
    assert len(shapes) == 1
    assert "dict" in shapes[0].pattern


def test_detect_all_combines_results() -> None:
    """detect_all should combine results from all detectors."""
    source = '''\
class Strict:
    def create(self, name):
        if not name:
            raise ValueError("required")
        return {"name": name}

    def update(self, id, data):
        if not id:
            raise ValueError("required")
        return {"id": id, "data": data}

    def delete(self, id):
        if id is None:
            raise ValueError("required")
        return {"deleted": True}
'''
    results = detect_all(source, scope="Strict")
    kinds = {c.kind for c in results}
    # Should detect guard_clause, error_type, and return_shape
    assert "guard_clause" in kinds
    assert "error_type" in kinds
    assert "return_shape" in kinds


def test_syntax_error_returns_empty() -> None:
    """Malformed source code -> empty results, no crash."""
    results = detect_all("def foo(:\n  pass")
    assert results == []


# ---------------------------------------------------------------------------
# ConventionFingerprint tests
# ---------------------------------------------------------------------------

from groundtruth.analysis.conventions import ConventionFingerprint, fingerprint_class


GUARDED_CLASS = '''
class Validator:
    def check_name(self, name):
        if not name:
            raise ValueError("required")
        return {"name": name}

    def check_email(self, email):
        if not email:
            raise ValueError("required")
        return {"email": email}

    def check_age(self, age):
        if age < 0:
            raise ValueError("too low")
        return {"age": age}
'''


def test_fingerprint_guarded_class() -> None:
    fp = fingerprint_class(GUARDED_CLASS, "Validator")
    assert fp.guard_clause_freq >= 0.7
    assert fp.error_type == "ValueError"
    assert fp.return_shape == "dict"


def test_fingerprint_empty_class() -> None:
    code = """
class Empty:
    pass
"""
    fp = fingerprint_class(code, "Empty")
    assert fp.guard_clause_freq == 0.0
    assert fp.error_type is None
    assert fp.return_shape is None


def test_fingerprint_hashable() -> None:
    """Fingerprints can be used in sets and as dict keys."""
    fp1 = fingerprint_class(GUARDED_CLASS, "Validator")
    fp2 = fingerprint_class(GUARDED_CLASS, "Validator")
    assert fp1 == fp2
    assert hash(fp1) == hash(fp2)
    assert len({fp1, fp2}) == 1


def test_fingerprint_different_classes_differ() -> None:
    other = """
class Plain:
    def foo(self):
        return 42
    def bar(self):
        return 43
    def baz(self):
        return 44
"""
    fp1 = fingerprint_class(GUARDED_CLASS, "Validator")
    fp2 = fingerprint_class(other, "Plain")
    assert fp1 != fp2


def test_fingerprint_syntax_error() -> None:
    fp = fingerprint_class("not valid python {{", "Foo")
    assert fp.guard_clause_freq == 0.0
    assert fp.error_type is None
