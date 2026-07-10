"""E capability — at-edit syntax validation engine (edit_check.py), Stage-1.

TTD, red-first: these tests were written against the MISSING module
``groundtruth.runtime.edit_check`` and watched fail before it existed.

They prove the two verbs of the engine:

1. ``check_edit_syntax`` — per-language dispatch with the POSITIVE-EVIDENCE law:
   a ``syntax_error`` verdict requires a non-zero exit AND an error-shaped
   diagnostic; a tool that is missing, times out, returns no exit code, or a
   language we cannot cheaply check returns ``unavailable`` (correct-or-quiet).
   node/gofmt/ruby are driven by FAKE executors (deterministic, cross-platform);
   the .py path is exercised in-process AND through a REAL subprocess executor.

2. ``caller_diff_advisory`` — verified (FACT-tier, conf>=0.7, non-test) callers of
   an edited symbol from graph.db, mirroring ``covering_runner`` query discipline;
   name_match guesses, low-confidence edges, and is_test callers are EXCLUDED
   (the leak-law), and a legacy no-confidence-column schema is tolerated.
"""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from groundtruth.runtime.edit_check import caller_diff_advisory, check_edit_syntax
from groundtruth.runtime.native_render import contains_gt_tag

# --- graph.db fixture (mirrors tests/runtime/test_b1_covering_selection.py) ---
_NODES_SCHEMA = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
    "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, "
    "signature TEXT, return_type TEXT, is_exported INT, is_test INT, "
    "language TEXT, parent_id INT)"
)
_EDGES_SCHEMA = (
    "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
    "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, "
    "confidence REAL, metadata TEXT)"
)


def _make_graph(path, edges, nodes):
    """nodes: (id, name, file_path, is_test); edges: (src, tgt, type, method, conf)."""
    con = sqlite3.connect(str(path))
    con.execute(_NODES_SCHEMA)
    con.execute(_EDGES_SCHEMA)
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
            "VALUES (?,?,?,?,?,?)",
            (nid, "Function", name, fpath, is_test, "python"),
        )
    for src, tgt, etype, method, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
            "VALUES (?,?,?,?,?)",
            (src, tgt, etype, method, conf),
        )
    con.commit()
    con.close()


def _fake_executor(exit_code, stdout="", stderr=""):
    """A deterministic executor honoring the frozen contract."""
    def _run(cmd, cwd, timeout):
        return (exit_code, stdout, stderr)
    return _run


def _real_local_executor(cmd, cwd, timeout):
    """Wrap the frozen Wave-1 subprocess runner so a test can force the SUBPROCESS
    path (byte-identical to executor=None internals) and prove the real dispatch."""
    from groundtruth.runtime.test_runner import _run_subprocess
    return _run_subprocess(cmd, cwd, timeout)


# ===========================================================================
# check_edit_syntax — .py in-process (executor=None), real end-to-end
# ===========================================================================
def test_valid_py_in_process_ok(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("def foo(x):\n    return x + 1\n")
    res = check_edit_syntax(str(f), str(tmp_path))
    assert res["verdict"] == "ok", res
    assert res["diagnostic"] == ""


def test_broken_py_in_process_syntax_error(tmp_path):
    """Unclosed paren -> syntax_error with the ast error text in the diagnostic."""
    f = tmp_path / "bad.py"
    f.write_text("x = (1 + 2\n")  # '(' never closed
    res = check_edit_syntax(str(f), str(tmp_path))
    assert res["verdict"] == "syntax_error", res
    assert "syntaxerror" in res["diagnostic"].lower(), res
    assert not contains_gt_tag(res["diagnostic"])


def test_indentation_error_py_in_process_syntax_error(tmp_path):
    f = tmp_path / "indent.py"
    f.write_text("def foo():\nreturn 1\n")  # body not indented
    res = check_edit_syntax(str(f), str(tmp_path))
    assert res["verdict"] == "syntax_error", res


def test_unreadable_py_file_unavailable(tmp_path):
    """A missing file cannot be parsed -> unavailable, never a guessed verdict."""
    res = check_edit_syntax(str(tmp_path / "nope.py"), str(tmp_path))
    assert res["verdict"] == "unavailable", res


# ===========================================================================
# check_edit_syntax — .py through a REAL subprocess executor (end-to-end)
# ===========================================================================
def test_broken_py_via_real_subprocess_executor(tmp_path):
    f = tmp_path / "bad2.py"
    f.write_text("def foo(:\n    pass\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_real_local_executor)
    assert res["verdict"] == "syntax_error", res
    assert "syntaxerror" in res["diagnostic"].lower(), res


def test_valid_py_via_real_subprocess_executor(tmp_path):
    f = tmp_path / "ok2.py"
    f.write_text("y = 41 + 1\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_real_local_executor)
    assert res["verdict"] == "ok", res


# ===========================================================================
# check_edit_syntax — .py error shape delivered through a FAKE executor
# ===========================================================================
def test_broken_py_via_fake_executor_syntax_error(tmp_path):
    f = tmp_path / "bad3.py"
    f.write_text("whatever\n")  # content irrelevant; the executor is faked
    traceback_text = (
        '  File "bad3.py", line 1\n'
        "    def foo(:\n"
        "           ^\n"
        "SyntaxError: invalid syntax\n"
    )
    res = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", traceback_text)
    )
    assert res["verdict"] == "syntax_error", res
    assert "invalid syntax" in res["diagnostic"].lower(), res


# ===========================================================================
# check_edit_syntax — JavaScript (fake executor, deterministic + cross-platform)
# ===========================================================================
def test_broken_js_via_fake_executor_syntax_error(tmp_path):
    f = tmp_path / "bad.js"
    f.write_text("const x = (\n")
    node_err = (
        "/x/bad.js:1\n"
        "const x = (\n"
        "          ^\n\n"
        "SyntaxError: Unexpected end of input\n"
        "    at wrapSafe (node:internal/modules/cjs/loader)\n"
    )
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(1, "", node_err))
    assert res["verdict"] == "syntax_error", res
    assert "syntaxerror" in res["diagnostic"].lower(), res


def test_valid_js_via_fake_executor_ok(tmp_path):
    f = tmp_path / "ok.js"
    f.write_text("const x = 1;\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(0, "", ""))
    assert res["verdict"] == "ok", res


@pytest.mark.parametrize("ext", [".mjs", ".cjs"])
def test_js_variants_dispatch(tmp_path, ext):
    f = tmp_path / f"mod{ext}"
    f.write_text("export const x = 1;\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(0, "", ""))
    assert res["verdict"] == "ok", res


# ===========================================================================
# check_edit_syntax — Go (gofmt -e) + Ruby (ruby -c), fake executors
# ===========================================================================
def test_broken_go_via_fake_executor_syntax_error(tmp_path):
    f = tmp_path / "bad.go"
    f.write_text("package main\nfunc {\n")
    gofmt_err = "bad.go:2:6: expected 'IDENT', found '{'\n"
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(2, "", gofmt_err))
    assert res["verdict"] == "syntax_error", res
    assert "expected" in res["diagnostic"].lower(), res


def test_broken_rb_via_fake_executor_syntax_error(tmp_path):
    f = tmp_path / "bad.rb"
    f.write_text("def foo\n")
    ruby_err = "bad.rb:2: syntax error, unexpected end-of-input, expecting `end'\n"
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(1, "", ruby_err))
    assert res["verdict"] == "syntax_error", res


def test_valid_go_via_fake_executor_ok(tmp_path):
    f = tmp_path / "ok.go"
    f.write_text("package main\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(0, "package main\n", ""))
    assert res["verdict"] == "ok", res


# ===========================================================================
# THE POSITIVE-EVIDENCE LAW — tool-missing / timeout / no-exit-code / ambiguous
# ===========================================================================
def test_tool_missing_empty_output_unavailable(tmp_path):
    """Non-zero exit with an EMPTY diagnostic is NOT positive evidence of a syntax
    error -> unavailable. MUTATION TARGET: drop the error-shape requirement (make
    non-zero exit alone => syntax_error) and this test bites."""
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(127, "", ""))
    assert res["verdict"] == "unavailable", res


def test_tool_missing_command_not_found_text_unavailable(tmp_path):
    """A 'command not found' diagnostic is an ENVIRONMENT failure, never a syntax
    error -> unavailable."""
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")
    res = check_edit_syntax(
        str(f), str(tmp_path),
        executor=_fake_executor(127, "", "node: command not found\n"),
    )
    assert res["verdict"] == "unavailable", res


def test_none_exit_code_unavailable(tmp_path):
    """No reliable exit code -> ambiguous -> unavailable (never a fabricated fail)."""
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")
    res = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(None, "some output", "")
    )
    assert res["verdict"] == "unavailable", res


def test_timeout_unavailable(tmp_path):
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")

    def _timeout_executor(cmd, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    res = check_edit_syntax(str(f), str(tmp_path), executor=_timeout_executor)
    assert res["verdict"] == "unavailable", res


def test_executor_raises_unavailable(tmp_path):
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")

    def _boom(cmd, cwd, timeout):
        raise RuntimeError("boom")

    res = check_edit_syntax(str(f), str(tmp_path), executor=_boom)
    assert res["verdict"] == "unavailable", res


def test_nonzero_exit_no_syntax_shape_unavailable(tmp_path):
    """Non-zero exit whose diagnostic is neither an env failure nor a syntax-error
    shape (e.g. a tool usage/help message) -> ambiguous -> unavailable."""
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")
    res = check_edit_syntax(
        str(f), str(tmp_path),
        executor=_fake_executor(1, "", "Usage: node [options] script.js\n"),
    )
    assert res["verdict"] == "unavailable", res


# ===========================================================================
# CORRECT-OR-QUIET — languages we cannot cheaply/soundly check are unavailable
# ===========================================================================
@pytest.mark.parametrize("name", ["mod.ts", "comp.tsx", "lib.rs", "App.java", "part.jsx", "readme.xyz", "noext"])
def test_unchecked_language_unavailable(tmp_path, name):
    f = tmp_path / name
    f.write_text("whatever content\n")
    # A checker must NOT be invoked for these; a raising executor proves no run.
    def _must_not_run(cmd, cwd, timeout):
        raise AssertionError(f"executor invoked for unsupported ext: {cmd}")

    res = check_edit_syntax(str(f), str(tmp_path), executor=_must_not_run)
    assert res["verdict"] == "unavailable", res


def test_empty_file_path_unavailable(tmp_path):
    assert check_edit_syntax("", str(tmp_path))["verdict"] == "unavailable"


# ===========================================================================
# diagnostic hygiene — bounded, no <gt-*>
# ===========================================================================
def test_diagnostic_bounded_and_no_gt_tag(tmp_path):
    f = tmp_path / "big.js"
    f.write_text("const x = (\n")
    huge = "SyntaxError: Unexpected end of input\n" + ("noise line <gt-evidence> filler\n" * 500)
    res = check_edit_syntax(str(f), str(tmp_path), executor=_fake_executor(1, "", huge))
    assert res["verdict"] == "syntax_error", res
    assert len(res["diagnostic"]) <= 1400, len(res["diagnostic"])
    assert not contains_gt_tag(res["diagnostic"]), res["diagnostic"]


# ===========================================================================
# REAL-TOOL smoke tests (skip-marked where the toolchain may be absent)
# ===========================================================================
@pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node not on PATH"
)
def test_real_node_check_broken_js(tmp_path):
    f = tmp_path / "rbad.js"
    f.write_text("const x = (\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_real_local_executor)
    assert res["verdict"] == "syntax_error", res


@pytest.mark.skipif(
    __import__("shutil").which("gofmt") is None, reason="gofmt not on PATH"
)
def test_real_gofmt_broken_go(tmp_path):
    f = tmp_path / "rbad.go"
    f.write_text("package main\nfunc {\n")
    res = check_edit_syntax(str(f), str(tmp_path), executor=_real_local_executor)
    assert res["verdict"] == "syntax_error", res


# ===========================================================================
# caller_diff_advisory — verified non-test callers, leak-safe
# ===========================================================================
def test_advisory_returns_fact_caller(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "handle_request", "src/api.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9)],
    )
    out = caller_diff_advisory(str(db), {"get_user"})
    assert len(out) == 1, out
    row = out[0]
    assert row["symbol"] == "get_user"
    assert row["caller"] == "handle_request"
    assert row["file"] == "src/api.py"
    assert row["confidence"] == 0.9


def test_advisory_excludes_name_match_guess(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "handle_request", "src/api.py", 0)],
        edges=[(2, 1, "CALLS", "name_match", 0.9)],  # guess, not a fact
    )
    assert caller_diff_advisory(str(db), {"get_user"}) == []


def test_advisory_excludes_low_confidence(tmp_path):
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "handle_request", "src/api.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.5)],  # FACT method but conf < 0.7
    )
    assert caller_diff_advisory(str(db), {"get_user"}) == []


def test_advisory_excludes_test_caller_LEAK(tmp_path):
    """LEAK-LAW: an is_test caller must NEVER be returned. MUTATION TARGET: drop
    the is_test exclusion and this test bites (test identity leaks into advisory)."""
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/users.py", 0),
               (2, "handle_request", "src/api.py", 0),      # prod caller
               (3, "test_get_user", "tests/test_users.py", 1)],  # test caller
        edges=[(2, 1, "CALLS", "import", 0.9),
               (3, 1, "CALLS", "import", 0.95)],
    )
    out = caller_diff_advisory(str(db), {"get_user"})
    callers = {r["caller"] for r in out}
    assert callers == {"handle_request"}, out
    assert "test_get_user" not in str(out)


def test_advisory_excludes_test_target(tmp_path):
    """The edited symbol itself, if a test node, is not an advisory subject."""
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "test_helper", "tests/t.py", 1),
               (2, "caller", "src/a.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9)],
    )
    assert caller_diff_advisory(str(db), {"test_helper"}) == []


def test_advisory_legacy_schema_no_confidence_column(tmp_path):
    """A graph.db predating the confidence column: FACT edges trusted at conf 1.0."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "file_path TEXT, is_test INT, language TEXT)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, resolution_method TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','get_user','src/users.py',0,'python')")
    con.execute("INSERT INTO nodes VALUES (2,'Function','handle','src/api.py',0,'python')")
    con.execute("INSERT INTO edges VALUES (1,2,1,'CALLS','import')")
    con.commit()
    con.close()
    out = caller_diff_advisory(str(db), {"get_user"})
    assert len(out) == 1 and out[0]["caller"] == "handle", out
    assert out[0]["confidence"] == 1.0


def test_advisory_legacy_schema_no_resolution_method_returns_empty(tmp_path):
    """No resolution_method column -> provenance unjudgeable -> correct-or-quiet []."""
    db = tmp_path / "noprov.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test INT)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'get_user','src/users.py',0)")
    con.execute("INSERT INTO nodes VALUES (2,'handle','src/api.py',0)")
    con.execute("INSERT INTO edges VALUES (1,2,1,'CALLS')")
    con.commit()
    con.close()
    assert caller_diff_advisory(str(db), {"get_user"}) == []


def test_advisory_empty_cases(tmp_path):
    assert caller_diff_advisory("", {"x"}) == []
    assert caller_diff_advisory(str(tmp_path / "missing.db"), {"x"}) == []
    db = tmp_path / "g.db"
    _make_graph(db, nodes=[(1, "f", "a.py", 0)], edges=[])
    assert caller_diff_advisory(str(db), set()) == []
    assert caller_diff_advisory(str(db), {"f"}) == []  # no covering edge


def test_advisory_per_symbol_tagging(tmp_path):
    """Multiple edited symbols keep their caller->symbol attribution."""
    db = tmp_path / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "alpha", "src/a.py", 0),
               (2, "beta", "src/b.py", 0),
               (3, "ca", "src/x.py", 0),
               (4, "cb", "src/y.py", 0)],
        edges=[(3, 1, "CALLS", "import", 0.8),
               (4, 2, "CALLS", "same_file", 0.9)],
    )
    out = caller_diff_advisory(str(db), {"alpha", "beta"})
    pairs = {(r["symbol"], r["caller"]) for r in out}
    assert pairs == {("alpha", "ca"), ("beta", "cb")}, out
