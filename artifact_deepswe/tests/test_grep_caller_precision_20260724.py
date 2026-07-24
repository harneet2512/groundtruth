"""AUDIT 2026-07-24 — precision of the GT_SIG_CALLER_FALLBACK repo search.

When a signature changes but graph.db indexed ZERO callers, GT falls back to a bounded repo text
search and tells the model "N call site(s) found via search ... update them". Every path in that
line is a CLAIM. The first cut searched whole-file text, so a `# target(` comment counted as a call
site — a fabricated claim in model-facing bytes, which correct-or-quiet forbids.

`gt_mini_patch` cannot be imported standalone (it is host-injected into a running harness), so this
extracts the function and exercises it directly — the same technique the module's other unit tests
use. What is asserted is PRECISION, because a false caller is worse here than no caller at all: the
agent would go edit a comment.
"""
from __future__ import annotations
import os
import re

_SRC = os.path.join(os.path.dirname(__file__), "..", "gt_mini_patch.py")


def _load_grep_callers():
    src = open(os.path.abspath(_SRC), encoding="utf-8").read()
    i = src.index("def _grep_callers(")
    j = src.index("\ndef ", i + 10)
    ns = {
        "re": re, "os": os,
        # the real predicate lives elsewhere in the module; the leak-relevant behaviour is
        # "anything test-shaped is skipped", which this mirrors faithfully for the fixture.
        "_is_post_search_testpath": lambda rel: (
            rel.startswith("test") or "/test" in rel or "_test." in rel
        ),
    }
    exec(src[i:j], ns)
    return ns["_grep_callers"]


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def target(x):\n    return x\n")
    (tmp_path / "pkg" / "b.py").write_text("from .a import target\n\ndef g():\n    return target(1)\n")
    (tmp_path / "pkg" / "c.py").write_text("import re\n# target( in a comment\n")
    (tmp_path / "pkg" / "d.py").write_text("obj.target(1)\n")
    (tmp_path / "tests" / "test_x.py").write_text("target(1)\n")
    return str(tmp_path)


def test_finds_the_real_caller(tmp_path):
    assert "pkg/b.py" in _load_grep_callers()("target", _repo(tmp_path), "pkg/a.py")


def test_a_comment_is_not_a_call_site(tmp_path):
    """The regression: the model must never be sent to edit a comment."""
    assert "pkg/c.py" not in _load_grep_callers()("target", _repo(tmp_path), "pkg/a.py")


def test_method_on_another_object_is_not_a_call_site(tmp_path):
    r"""`obj.target(` is a DIFFERENT symbol — the (?<![\w.]) guard must hold."""
    assert "pkg/d.py" not in _load_grep_callers()("target", _repo(tmp_path), "pkg/a.py")


def test_test_paths_are_never_reported(tmp_path):
    """LEAK-LAW: a test path in model-facing bytes is a leak, full stop."""
    hits = _load_grep_callers()("target", _repo(tmp_path), "pkg/a.py")
    assert not any("test" in h for h in hits), hits


def test_the_edited_file_excludes_itself(tmp_path):
    assert "pkg/a.py" not in _load_grep_callers()("target", _repo(tmp_path), "pkg/a.py")


def test_absent_or_invalid_symbol_stays_quiet(tmp_path):
    g = _load_grep_callers()
    root = _repo(tmp_path)
    assert g("nosuchsymbol", root, "pkg/a.py") == []
    assert g("not an identifier", root, "pkg/a.py") == []
    assert g("", root, "pkg/a.py") == []
    assert g("target", "", "pkg/a.py") == []


def test_result_is_bounded(tmp_path):
    """Bounded output: the contract line must never flood the model."""
    root = _repo(tmp_path)
    for n in range(20):
        (tmp_path / "pkg" / f"m{n}.py").write_text("target(1)\n")
    assert len(_load_grep_callers()("target", root, "pkg/a.py")) <= 8
