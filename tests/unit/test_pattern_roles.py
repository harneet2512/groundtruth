"""Tests for pattern role classification."""

from __future__ import annotations

from groundtruth.analysis.pattern_roles import (
    COMPARES_IN_EQ,
    EMITS_TO_OUTPUT,
    GUARDS_ON_STATE,
    SERIALIZES_TO_KWARGS,
    STORES_IN_STATE,
    VALIDATES_INPUT,
    classify_method_role,
    classify_roles_for_obligation,
)

STORES_CODE = """
class Foo:
    def __init__(self):
        self.x = 0
    def update(self):
        self.x = 42
"""

STORES_AUGASSIGN_CODE = """
class Foo:
    def __init__(self):
        self.count = 0
    def bump(self):
        self.count += 1
"""

SERIALIZES_CODE = """
class Foo:
    def __init__(self):
        self.fields = []
    def deconstruct(self):
        return ('app.Foo', [], {'fields': self.fields})
"""

COMPARES_CODE = """
class Foo:
    def __init__(self):
        self.x = 0
    def __eq__(self, other):
        return self.x == other.x
"""

VALIDATES_CODE = """
class Foo:
    def __init__(self):
        self.data = []
    def process(self):
        validate(self.data)
"""

VALIDATES_KWARG_CODE = """
class Foo:
    def __init__(self):
        self.name = ''
    def save(self):
        db.insert(name=self.name)
"""

GUARDS_CODE = """
class Foo:
    def __init__(self):
        self.ready = False
    def run(self):
        if self.ready:
            do_work()
"""

EMITS_FSTRING_CODE = """
class Foo:
    def __init__(self):
        self.name = ''
    def __repr__(self):
        return f'Foo(name={self.name})'
"""

EMITS_PERCENT_CODE = """
class Foo:
    def __init__(self):
        self.name = ''
    def label(self):
        return 'Name: %s' % self.name
"""

MULTIPLE_ROLES_CODE = """
class Foo:
    def __init__(self):
        self.x = 0
    def transform(self):
        if self.x:
            validate(self.x)
        self.x = self.x + 1
        return self.x
"""

NO_USAGE_CODE = """
class Foo:
    def __init__(self):
        self.x = 0
    def unrelated(self):
        y = 42
        return y
"""


def test_stores_in_state() -> None:
    roles = classify_method_role(STORES_CODE, "update", "x")
    assert STORES_IN_STATE in roles


def test_stores_in_state_augassign() -> None:
    roles = classify_method_role(STORES_AUGASSIGN_CODE, "bump", "count")
    assert STORES_IN_STATE in roles


def test_serializes_to_kwargs() -> None:
    roles = classify_method_role(SERIALIZES_CODE, "deconstruct", "fields")
    assert SERIALIZES_TO_KWARGS in roles


def test_compares_in_eq() -> None:
    roles = classify_method_role(COMPARES_CODE, "__eq__", "x")
    assert COMPARES_IN_EQ in roles


def test_validates_input_positional() -> None:
    roles = classify_method_role(VALIDATES_CODE, "process", "data")
    assert VALIDATES_INPUT in roles


def test_validates_input_keyword() -> None:
    roles = classify_method_role(VALIDATES_KWARG_CODE, "save", "name")
    assert VALIDATES_INPUT in roles


def test_guards_on_state() -> None:
    roles = classify_method_role(GUARDS_CODE, "run", "ready")
    assert GUARDS_ON_STATE in roles


def test_emits_to_output_fstring() -> None:
    roles = classify_method_role(EMITS_FSTRING_CODE, "__repr__", "name")
    assert EMITS_TO_OUTPUT in roles


def test_emits_to_output_percent() -> None:
    roles = classify_method_role(EMITS_PERCENT_CODE, "label", "name")
    assert EMITS_TO_OUTPUT in roles


def test_multiple_roles_in_one_method() -> None:
    roles = classify_method_role(MULTIPLE_ROLES_CODE, "transform", "x")
    assert STORES_IN_STATE in roles
    assert SERIALIZES_TO_KWARGS in roles
    assert GUARDS_ON_STATE in roles
    assert VALIDATES_INPUT in roles
    assert len(roles) == 4


def test_no_usage_returns_empty() -> None:
    roles = classify_method_role(NO_USAGE_CODE, "unrelated", "x")
    assert roles == []


def test_method_not_found_returns_empty() -> None:
    roles = classify_method_role(STORES_CODE, "nonexistent", "x")
    assert roles == []


def test_unparseable_source_returns_empty() -> None:
    roles = classify_method_role("def broken(:\n  pass", "broken", "x")
    assert roles == []


def test_classify_roles_for_obligation() -> None:
    result = classify_roles_for_obligation(
        MULTIPLE_ROLES_CODE, "transform", ["x"]
    )
    assert "x" in result
    assert STORES_IN_STATE in result["x"]
    assert SERIALIZES_TO_KWARGS in result["x"]


def test_classify_roles_for_obligation_multiple_attrs() -> None:
    code = """
class Foo:
    def __init__(self):
        self.a = 0
        self.b = ''
    def work(self):
        self.a = 1
        return self.b
"""
    result = classify_roles_for_obligation(code, "work", ["a", "b"])
    assert STORES_IN_STATE in result["a"]
    assert SERIALIZES_TO_KWARGS in result["b"]
    assert STORES_IN_STATE not in result["b"]


# ---------------------------------------------------------------------------
# StateFlowGraph tests
# ---------------------------------------------------------------------------

from groundtruth.analysis.pattern_roles import StateFlowGraph, build_state_flow


MULTI_METHOD_CLASS = """
class CacheManager:
    def __init__(self, data, ttl):
        self.data = data
        self.ttl = ttl

    def get(self, key):
        if self.ttl > 0:
            return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def serialize(self):
        return {"data": self.data, "ttl": self.ttl}

    def __eq__(self, other):
        return self.data == other.data and self.ttl == other.ttl
"""


def test_state_flow_basic() -> None:
    graph = build_state_flow(MULTI_METHOD_CLASS, "CacheManager")
    # data is used across multiple methods
    assert "data" in graph.attr_to_methods
    assert "__init__" in graph.attr_to_methods["data"]
    assert STORES_IN_STATE in graph.attr_to_methods["data"]["__init__"]
    assert "serialize" in graph.attr_to_methods["data"]
    assert SERIALIZES_TO_KWARGS in graph.attr_to_methods["data"]["serialize"]
    assert "__eq__" in graph.attr_to_methods["data"]
    assert COMPARES_IN_EQ in graph.attr_to_methods["data"]["__eq__"]


def test_state_flow_method_to_attrs() -> None:
    graph = build_state_flow(MULTI_METHOD_CLASS, "CacheManager")
    # __init__ stores both data and ttl
    assert "data" in graph.method_to_attrs["__init__"]
    assert "ttl" in graph.method_to_attrs["__init__"]


def test_state_flow_guards() -> None:
    graph = build_state_flow(MULTI_METHOD_CLASS, "CacheManager")
    # ttl is used in a guard in get()
    assert "ttl" in graph.attr_to_methods
    assert "get" in graph.attr_to_methods["ttl"]
    assert GUARDS_ON_STATE in graph.attr_to_methods["ttl"]["get"]


def test_state_flow_missing_class() -> None:
    graph = build_state_flow(MULTI_METHOD_CLASS, "NonExistent")
    assert graph.attr_to_methods == {}
    assert graph.method_to_attrs == {}


def test_state_flow_no_self_refs() -> None:
    code = """
class Empty:
    def do_stuff(self):
        x = 1 + 2
        return x
"""
    graph = build_state_flow(code, "Empty")
    assert graph.attr_to_methods == {}
    assert graph.method_to_attrs == {}


def test_state_flow_syntax_error() -> None:
    graph = build_state_flow("this is not valid python {{{{", "Foo")
    assert graph.attr_to_methods == {}


def test_state_flow_dual_role() -> None:
    """Attribute used as both source and sink in same method."""
    code = """
class Counter:
    def __init__(self):
        self.count = 0
    def increment(self):
        self.count = self.count + 1
        return self.count
"""
    graph = build_state_flow(code, "Counter")
    roles = graph.attr_to_methods["count"]["increment"]
    assert STORES_IN_STATE in roles
    assert SERIALIZES_TO_KWARGS in roles
