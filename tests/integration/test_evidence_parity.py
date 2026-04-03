"""Evidence parity tests — verify non-Python languages get evidence.

Tests that CHANGE, PATTERN, and CONTRACT evidence families produce
meaningful output for Go and TypeScript, not just Python. Uses regex
and graph.db paths since we can't run gt-index in CI without CGO.
"""

import pytest
from groundtruth.evidence.change import (
    ChangeAnalyzer,
    ChangeEvidence,
    _find_function_in_source,
    _regex_classify_return_shape,
    _regex_extract_catch_handlers,
    _regex_extract_exceptions,
    _regex_extract_guards,
    _regex_detect_swallowed,
    _normalize_shape,
)
from groundtruth.evidence.pattern import PatternEvidence, SiblingAnalyzer
from groundtruth.evidence.contract import (
    TestAssertionMiner,
    _is_test_file,
)


# ── Guard extraction tests (language-agnostic) ──────────────────────────

class TestRegexExtractGuards:
    def test_python_guard(self):
        body = '''def foo(x):
    if x is None:
        raise ValueError("x required")
    return x + 1'''
        guards = _regex_extract_guards(body)
        assert len(guards) >= 1
        assert guards[0][0] == "raise"

    def test_go_guard(self):
        body = '''func Foo(x int) error {
    if x <= 0 {
        return fmt.Errorf("x must be positive")
    }
    return nil
}'''
        guards = _regex_extract_guards(body)
        assert len(guards) >= 1
        assert guards[0][0] == "return"

    def test_java_guard(self):
        body = '''public User getById(int id) {
    if (id <= 0) {
        throw new IllegalArgumentException("Invalid id");
    }
    return repository.findById(id);
}'''
        guards = _regex_extract_guards(body)
        assert len(guards) >= 1
        assert guards[0][0] == "raise"  # throw maps to raise

    def test_rust_guard(self):
        body = '''fn get_user(id: i32) -> Result<User, Error> {
    if id <= 0 {
        return Err(Error::InvalidId);
    }
    Ok(User::find(id))
}'''
        guards = _regex_extract_guards(body)
        assert len(guards) >= 1

    def test_no_guard(self):
        body = '''def foo(x):
    return x + 1'''
        guards = _regex_extract_guards(body)
        assert len(guards) == 0


# ── Exception extraction tests ───────────────────────────────────────────

class TestRegexExtractExceptions:
    def test_python_raise(self):
        body = "raise ValueError('bad')"
        exc = _regex_extract_exceptions(body)
        assert "ValueError" in exc

    def test_java_throw(self):
        body = "throw new IllegalArgumentException('bad');"
        exc = _regex_extract_exceptions(body)
        assert "IllegalArgumentException" in exc

    def test_go_panic(self):
        body = "panic(\"something went wrong\")"
        exc = _regex_extract_exceptions(body)
        assert "panic" in exc

    def test_go_error_return(self):
        body = "return fmt.Errorf(\"failed: %w\", err)"
        exc = _regex_extract_exceptions(body)
        assert "error" in exc


# ── Catch handler extraction tests ───────────────────────────────────────

class TestRegexExtractCatchHandlers:
    def test_python_except(self):
        body = "except ValueError as e:\n    log(e)"
        handlers = _regex_extract_catch_handlers(body)
        assert "ValueError" in handlers

    def test_python_bare_except(self):
        body = "except:\n    pass"
        handlers = _regex_extract_catch_handlers(body)
        assert "bare_except" in handlers

    def test_java_catch(self):
        body = "catch (IOException e) {\n    log(e);\n}"
        handlers = _regex_extract_catch_handlers(body)
        assert "IOException" in handlers


# ── Swallowed exception detection ────────────────────────────────────────

class TestRegexDetectSwallowed:
    def test_python_pass(self):
        body = "except ValueError:\n    pass"
        assert _regex_detect_swallowed(body) is True

    def test_java_empty_catch(self):
        body = "catch (Exception e) { }"
        assert _regex_detect_swallowed(body) is True

    def test_not_swallowed(self):
        body = "except ValueError as e:\n    logger.error(e)"
        assert _regex_detect_swallowed(body) is False


# ── Return shape classification ──────────────────────────────────────────

class TestRegexReturnShape:
    def test_tuple(self):
        body = "return (a, b, c)"
        assert _regex_classify_return_shape(body) == "tuple"

    def test_go_multi_return(self):
        body = "return user, nil"
        assert _regex_classify_return_shape(body) == "tuple"

    def test_value(self):
        body = "return result"
        assert _regex_classify_return_shape(body) == "value"

    def test_none(self):
        body = "return None"
        assert _regex_classify_return_shape(body) == "none"

    def test_nil(self):
        body = "return nil"
        assert _regex_classify_return_shape(body) == "none"


# ── Shape normalization ──────────────────────────────────────────────────

class TestNormalizeShape:
    def test_scalar_to_value(self):
        assert _normalize_shape("scalar") == "value"

    def test_none_variants(self):
        assert _normalize_shape("None") == "none"
        assert _normalize_shape("nil") == "none"
        assert _normalize_shape("null") == "none"

    def test_tuple_with_count(self):
        assert _normalize_shape("tuple(2)") == "tuple"
        assert _normalize_shape("tuple(3)") == "tuple"

    def test_passthrough(self):
        assert _normalize_shape("value") == "value"
        assert _normalize_shape("tuple") == "tuple"


# ── Function boundary detection ──────────────────────────────────────────

class TestFindFunctionInSource:
    def test_python(self):
        source = '''def foo():
    pass

def bar(x):
    if x is None:
        raise ValueError
    return x + 1

def baz():
    pass'''
        start, end, body = _find_function_in_source(source, "bar", "python")
        assert start > 0
        assert "raise ValueError" in body
        assert "def bar" in body

    def test_go(self):
        source = '''package main

func Foo() {
    fmt.Println("foo")
}

func Bar(x int) error {
    if x <= 0 {
        return fmt.Errorf("bad")
    }
    return nil
}'''
        start, end, body = _find_function_in_source(source, "Bar", "go")
        assert start > 0
        assert "Errorf" in body

    def test_javascript(self):
        source = '''function foo() {
    console.log("foo");
}

function bar(x) {
    if (!x) {
        throw new Error("missing");
    }
    return x + 1;
}'''
        start, end, body = _find_function_in_source(source, "bar", "javascript")
        assert start > 0
        assert "throw" in body

    def test_not_found(self):
        source = "def foo(): pass"
        start, end, body = _find_function_in_source(source, "nonexistent", "python")
        assert start == -1
        assert body == ""


# ── Test file detection (language-agnostic) ──────────────────────────────

class TestIsTestFile:
    def test_python(self):
        assert _is_test_file("tests/test_auth.py") is True
        assert _is_test_file("test_utils.py") is True

    def test_go(self):
        assert _is_test_file("auth/jwt_test.go") is True

    def test_javascript(self):
        assert _is_test_file("src/auth/jwt.test.ts") is True
        assert _is_test_file("src/__tests__/auth.js") is True

    def test_java(self):
        assert _is_test_file("src/test/java/UserTest.java") is True

    def test_csharp(self):
        assert _is_test_file("UserTests.cs") is True

    def test_ruby(self):
        assert _is_test_file("spec/auth_spec.rb") is True

    def test_not_test(self):
        assert _is_test_file("src/auth.py") is False
        assert _is_test_file("main.go") is False


# ── SiblingAnalyzer Python AST fallback ──────────────────────────────────

class TestSiblingAnalyzerPythonFallback:
    """Verify the Python AST path still produces evidence (regression guard)."""

    def test_missing_guard_detected(self):
        source = '''
class UserService:
    def create(self, data):
        if not data:
            raise ValueError("empty")
        return User(**data)

    def update(self, user_id, data):
        if not data:
            raise ValueError("empty")
        return User.update(user_id, **data)

    def delete(self, user_id):
        if not user_id:
            raise ValueError("missing id")
        return User.delete(user_id)

    def get(self, user_id):
        return User.find(user_id)
'''
        analyzer = SiblingAnalyzer()
        findings = analyzer.analyze(source, "get", file_path="test.py")
        guard_findings = [f for f in findings if f.kind == "missing_guard"]
        assert len(guard_findings) >= 1, f"Expected missing_guard, got: {[f.kind for f in findings]}"

    def test_return_shape_outlier(self):
        source = '''
def get_name():
    return "Alice"

def get_age():
    return 30

def get_email():
    return "alice@example.com"

def get_info():
    return {"name": "Alice", "age": 30}
'''
        analyzer = SiblingAnalyzer()
        findings = analyzer.analyze(source, "get_info", file_path="test.py")
        # get_info returns dict while others return scalar
        shape_findings = [f for f in findings if f.kind == "return_shape_outlier"]
        assert len(shape_findings) >= 1, f"Expected return_shape_outlier, got: {[f.kind for f in findings]}"


# ── TestAssertionMiner regex fallback ────────────────────────────────────

class TestAssertionMinerRegex:
    """Verify regex fallback produces assertions for non-Python test files."""

    def test_go_assertions(self, tmp_path):
        test_file = tmp_path / "jwt_test.go"
        test_file.write_text('''func TestSignToken(t *testing.T) {
    token, err := SignToken(payload)
    require.NoError(t, err)
    assert.NotEmpty(t, token)
    assert.Contains(t, token, ".")
}''')
        miner = TestAssertionMiner(str(tmp_path))
        results = miner.mine("jwt.go", [str(test_file.name)])
        assert len(results) >= 1, "Expected regex assertions from Go test file"

    def test_js_assertions(self, tmp_path):
        test_file = tmp_path / "jwt.test.ts"
        test_file.write_text('''describe('signToken', () => {
    it('returns valid JWT', () => {
        expect(token).toBeDefined();
        expect(token.split('.')).toHaveLength(3);
    });
});''')
        miner = TestAssertionMiner(str(tmp_path))
        results = miner.mine("jwt.ts", [str(test_file.name)])
        assert len(results) >= 1, "Expected regex assertions from TS test file"
