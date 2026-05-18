"""Unit tests for the semantic check module (guard comparison + return paths).

Tests both the standalone module (groundtruth.hooks.semantic_check) and the
_regex_extract_guards function from evidence/change.py that the behavioral
contract uses.
"""
from __future__ import annotations

import pytest

from groundtruth.hooks.semantic_check import (
    extract_guards,
    extract_return_paths,
    run_check,
)
from groundtruth.evidence.change import _regex_extract_guards


# ────────────────────────────────────────────────────
# extract_guards (semantic_check module)
# ────────────────────────────────────────────────────

class TestExtractGuards:
    def test_basic_guard_with_return(self):
        code = "def f(x):\n    if x is None:\n        return None\n    return x"
        guards = extract_guards(code)
        assert "x is None" in guards

    def test_guard_with_raise(self):
        code = "def f(x):\n    if not x:\n        raise ValueError('empty')\n    return x"
        guards = extract_guards(code)
        assert any("not x" in g for g in guards)

    def test_guard_with_throw_python(self):
        code = "def f(x):\n    if not x:\n        throw_error('bad')\n        return\n"
        guards = extract_guards(code)
        assert len(guards) >= 1

    def test_no_guards(self):
        code = "def f(x):\n    y = x + 1\n    return y"
        guards = extract_guards(code)
        assert len(guards) == 0

    def test_multiple_guards(self):
        code = (
            "def colorize(text):\n"
            "    if os.environ.get('FORCE_COLOR'):\n"
            "        return True\n"
            "    if not sys.stderr.isatty():\n"
            "        return False\n"
            "    return text\n"
        )
        guards = extract_guards(code)
        assert len(guards) == 2

    def test_non_guard_if_far_from_return(self):
        """If return is >200 chars away from the if, it's not a guard."""
        padding = "    " + "y = x * 2\n" * 25
        code = f"def f(x):\n    if x > 0:\n{padding}    return y"
        guards = extract_guards(code)
        assert len(guards) == 0


# ────────────────────────────────────────────────────
# extract_return_paths (semantic_check module)
# ────────────────────────────────────────────────────

class TestExtractReturnPaths:
    def test_basic_returns(self):
        code = "def f(x):\n    if x:\n        return True\n    return False"
        paths = extract_return_paths(code)
        assert paths == ["return True", "return False"]

    def test_bare_return(self):
        code = "def f(x):\n    if x:\n        return\n    pass"
        paths = extract_return_paths(code)
        assert paths == ["return"]

    def test_no_returns(self):
        code = "def f(x):\n    print(x)"
        paths = extract_return_paths(code)
        assert paths == []

    def test_return_complex(self):
        code = "def f(x):\n    return {'key': x, 'other': x + 1}"
        paths = extract_return_paths(code)
        assert len(paths) == 1


# ────────────────────────────────────────────────────
# run_check (semantic_check module — full pipeline)
# ────────────────────────────────────────────────────

class TestRunCheck:
    def test_returns_empty_for_missing_file(self, tmp_path):
        result = run_check("nonexistent.py", str(tmp_path))
        assert result == []

    def test_returns_return_paths_for_existing_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def foo(x):\n    return x + 1\n    return None\n")
        result = run_check("test.py", str(tmp_path))
        return_lines = [r for r in result if r.startswith("RETURN_PATH:")]
        assert len(return_lines) == 2

    def test_detects_added_guard(self, tmp_path):
        """Simulates a git repo where old content has no guard but new does."""
        import subprocess
        f = tmp_path / "test.py"
        f.write_text("def foo(x):\n    return x + 1\n")
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True,
            env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
                 "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
                 "GIT_COMMITTER_EMAIL": "t@t"},
        )
        f.write_text(
            "def foo(x):\n"
            "    if x is None:\n"
            "        return None\n"
            "    return x + 1\n"
        )
        result = run_check("test.py", str(tmp_path))
        guard_added = [r for r in result if r.startswith("GUARD_ADDED:")]
        assert len(guard_added) >= 1
        assert any("x is None" in g for g in guard_added)


# ────────────────────────────────────────────────────
# _regex_extract_guards (evidence/change.py — behavioral contract)
# ────────────────────────────────────────────────────

class TestRegexExtractGuards:
    def test_loguru_colorize_pattern(self):
        code = (
            "def colorize(text, color):\n"
            "    if os.environ.get('FORCE_COLOR'):\n"
            "        return True\n"
            "    if not sys.stderr.isatty():\n"
            "        return False\n"
            "    try:\n"
            "        import colorama\n"
            "    except ImportError:\n"
            "        return text\n"
            "    return colorama.Style.RESET_ALL + text\n"
        )
        guards = _regex_extract_guards(code)
        assert len(guards) >= 2
        types = [g[0] for g in guards]
        assert "return" in types

    def test_gate_fires(self):
        """The behavioral contract gate is len(guards) >= 2 or len(returns) >= 3."""
        code = (
            "def process(data):\n"
            "    if data is None:\n"
            "        raise ValueError\n"
            "    if not data.valid:\n"
            "        return False\n"
            "    return True\n"
        )
        guards = _regex_extract_guards(code)
        assert len(guards) >= 2, f"Expected >=2 guards, got {len(guards)}: {guards}"

    def test_no_guards_shallow_function(self):
        code = "def add(a, b):\n    return a + b\n"
        guards = _regex_extract_guards(code)
        assert len(guards) == 0
