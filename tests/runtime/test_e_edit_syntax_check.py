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
import traceback

import pytest

from groundtruth.runtime.edit_check import caller_diff_advisory, check_edit_syntax
from groundtruth.runtime.native_render import contains_gt_tag


# Profile-2 activates the stable diagnostic refinement in production. Tests in
# this module exercise that posture unless a case explicitly proves the OFF arm.
@pytest.fixture(autouse=True)
def _stable_python_diagnostic_profile_member(monkeypatch):
    monkeypatch.setenv("GT_SS_EDIT_DIAG", "1")


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


def test_python_syntax_diagnostic_is_interpreter_version_stable(tmp_path):
    """Python 3.10 subprocess frames and in-process parsing render identical bytes."""
    f = tmp_path / "stable.py"
    f.write_text("def foo():\nreturn 1\n", encoding="utf-8")

    local = check_edit_syntax(str(f), str(tmp_path))
    py310 = (
        'Traceback (most recent call last):\n'
        '  File "<string>", line 1, in <module>\n'
        '  File "/usr/local/lib/python3.10/ast.py", line 50, in parse\n'
        '    return compile(source, filename, mode, flags,\n'
        f'  File "/testbed/{f.name}", line 2\n'
        "    return 1\n"
        "    ^\n"
        "IndentationError: expected an indented block after function definition on line 1\n"
    )
    container = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", py310)
    )

    assert local["verdict"] == container["verdict"] == "syntax_error"
    assert local["diagnostic"] == container["diagnostic"]
    assert f'File "{f.name}", line 2' in local["diagnostic"]
    assert "ast.py" not in local["diagnostic"]


def test_python_syntax_diagnostic_flag_off_preserves_recorded_bytes(tmp_path, monkeypatch):
    """The SS refinement kill-switch preserves the pre-SS model-facing bytes."""
    monkeypatch.setenv("GT_SS_EDIT_DIAG", "0")
    f = tmp_path / "_random.py"
    f.write_text("value = 1\n", encoding="utf-8")
    recorded = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        '  File "/usr/local/lib/python3.10/ast.py", line 50, in parse\n'
        "    return compile(source, filename, mode, flags,\n"
        '  File "/testbed/_random.py", line 88\n'
        "                    mask = shapely_contains(geom, batch)\n"
        "                   ^\n"
        "IndentationError: unexpected indent\n"
    )

    result = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", recorded)
    )

    assert result["verdict"] == "syntax_error"
    assert result["diagnostic"] == recorded.strip()


def test_python_syntax_diagnostic_flag_off_preserves_in_process_bytes(
    tmp_path, monkeypatch
):
    """The explicit legacy switch applies when no live-env executor is available too."""
    monkeypatch.setenv("GT_SS_EDIT_DIAG", "0")
    f = tmp_path / "in_process.py"
    f.write_text("                    mask = value\n", encoding="utf-8")
    try:
        compile(f.read_bytes(), f.name, "exec", flags=0, dont_inherit=True)
    except SyntaxError as exc:
        expected = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    else:  # pragma: no cover - the fixture is intentionally invalid Python
        pytest.fail("invalid Python fixture unexpectedly compiled")

    result = check_edit_syntax(str(f), str(tmp_path))

    assert result["verdict"] == "syntax_error"
    assert result["diagnostic"] == expected


@pytest.mark.parametrize(
    ("profile", "stable"),
    [("2", True), ("0", False)],
)
def test_python_syntax_diagnostic_defaults_follow_effective_profile(
    tmp_path, monkeypatch, profile, stable
):
    """The internal refinement follows Profile-2 without becoming a CAP member."""
    monkeypatch.delenv("GT_SS_EDIT_DIAG", raising=False)
    monkeypatch.setenv("GT_RL_PROFILE", profile)
    f = tmp_path / "profiled.py"
    f.write_text("value = 1\n", encoding="utf-8")
    native = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        '  File "/usr/local/lib/python3.10/ast.py", line 50, in parse\n'
        "    return compile(source, filename, mode, flags,\n"
        '  File "/testbed/profiled.py", line 9\n'
        "    value = (\n"
        "            ^\n"
        "SyntaxError: '(' was never closed\n"
    )

    result = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", native)
    )

    assert ("ast.py" not in result["diagnostic"]) is stable
    if not stable:
        assert result["diagnostic"] == native.strip()


def test_python_syntax_diagnostic_does_not_fabricate_from_internal_frame(tmp_path):
    f = tmp_path / "stable.py"
    f.write_text("def ok():\n    return 1\n", encoding="utf-8")
    internal_only = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        '  File "/usr/local/lib/python3.10/ast.py", line 50, in parse\n'
        "    return compile(source, filename, mode, flags,\n"
        "SyntaxError: invalid syntax\n"
    )

    result = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", internal_only)
    )

    assert result["verdict"] == "syntax_error"
    assert result["diagnostic"] == "SyntaxError: invalid syntax"
    assert f.name not in result["diagnostic"]
    assert "line 50" not in result["diagnostic"]


def test_python_syntax_diagnostic_ignores_different_source_frame(tmp_path):
    f = tmp_path / "expected.py"
    f.write_text("value = 1\n", encoding="utf-8")
    wrong_file = (
        '  File "/testbed/other.py", line 17\n'
        "    value = (\n"
        "            ^\n"
        "SyntaxError: '(' was never closed\n"
    )

    result = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", wrong_file)
    )

    assert result["diagnostic"] == "SyntaxError: '(' was never closed"


@pytest.mark.parametrize(
    "container_root",
    ["/testbed", "/home/user", "/workspace", "/app", "/repo"],
)
def test_python_syntax_diagnostic_accepts_exact_container_mount_identity(
    tmp_path, container_root
):
    pkg = tmp_path / ".pkg"
    pkg.mkdir()
    f = pkg / ".hidden.py"
    f.write_text("value = 1\n", encoding="utf-8")
    native = (
        f'  File "{container_root}/.pkg/.hidden.py", line 4\n'
        "    value = (\n"
        "            ^\n"
        "SyntaxError: '(' was never closed\n"
    )

    result = check_edit_syntax(
        ".pkg/.hidden.py", str(tmp_path), executor=_fake_executor(1, "", native)
    )

    assert result["diagnostic"].startswith('File ".pkg/.hidden.py", line 4\n')


def test_python_syntax_diagnostic_normalizes_explicit_relative_segment(tmp_path):
    f = tmp_path / "pkg" / ".hidden.py"
    f.parent.mkdir()
    f.write_text("value = 1\n", encoding="utf-8")
    native = (
        '  File "/testbed/pkg/.hidden.py", line 4\n'
        "    value = (\n"
        "            ^\n"
        "SyntaxError: '(' was never closed\n"
    )

    result = check_edit_syntax(
        "./pkg/.hidden.py", str(tmp_path), executor=_fake_executor(1, "", native)
    )

    assert result["diagnostic"].startswith('File "./pkg/.hidden.py", line 4\n')


@pytest.mark.parametrize(
    "wrong_frame",
    [
        "/elsewhere/pkg/same.py",       # same relative suffix, wrong mount
        "/testbed/hidden.py",           # must not strip the leading dot
    ],
)
def test_python_syntax_diagnostic_rejects_colliding_frame_identity(
    tmp_path, wrong_frame
):
    rel = "pkg/same.py" if "same.py" in wrong_frame else ".hidden.py"
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("value = 1\n", encoding="utf-8")
    native = (
        f'  File "{wrong_frame}", line 9\n'
        "    value = (\n"
        "            ^\n"
        "SyntaxError: '(' was never closed\n"
    )

    result = check_edit_syntax(
        rel, str(tmp_path), executor=_fake_executor(1, "", native)
    )

    assert result["diagnostic"] == "SyntaxError: '(' was never closed"


def test_python_syntax_diagnostic_requires_matching_final_source_frame(tmp_path):
    f = tmp_path / "pkg" / "target.py"
    f.parent.mkdir()
    f.write_text("value = 1\n", encoding="utf-8")
    native = (
        '  File "/testbed/pkg/target.py", line 9\n'
        "    value = (\n"
        "            ^\n"
        '  File "/usr/local/lib/python3.10/ast.py", line 50, in parse\n'
        "    return compile(source, filename, mode, flags)\n"
        "SyntaxError: invalid syntax\n"
    )

    result = check_edit_syntax(
        "pkg/target.py", str(tmp_path), executor=_fake_executor(1, "", native)
    )

    assert result["diagnostic"] == "SyntaxError: invalid syntax"


def test_python_tabbed_diagnostic_is_host_container_byte_stable(tmp_path):
    f = tmp_path / "tabbed.py"
    f.write_bytes(b"def f():\n\treturn (\n")
    local = check_edit_syntax(str(f), str(tmp_path))
    native = (
        '  File "/testbed/tabbed.py", line 2\n'
        "    \treturn (\n"
        "    \t       ^\n"
        "SyntaxError: '(' was never closed\n"
    )

    container = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, "", native)
    )

    assert local["diagnostic"].encode() == container["diagnostic"].encode()
    assert "    \t       ^" in local["diagnostic"]


def test_python_declared_source_encoding_is_honored(tmp_path):
    f = tmp_path / "latin1.py"
    f.write_bytes(b"# coding: latin-1\ncaf\xe9 = 1\n")

    result = check_edit_syntax(str(f), str(tmp_path))

    assert result["verdict"] == "ok", result


def test_python_executor_command_parses_raw_bytes(tmp_path):
    f = tmp_path / "latin1.py"
    f.write_bytes(b"# coding: latin-1\ncaf\xe9 = 1\n")
    seen = []

    def executor(cmd, cwd, timeout):
        seen.append(cmd)
        return 0, "", ""

    assert check_edit_syntax(str(f), str(tmp_path), executor=executor)["verdict"] == "ok"
    command = seen[0]
    assert command[:3] == ["python", "-I", "-c"]
    command_source = command[command.index("-c") + 1]
    assert "'rb'" in command_source
    assert "errors='replace'" not in command_source


def test_python_subprocess_parser_isolated_from_repo_module_shadow(tmp_path):
    """A broken repo-local ``ast.py`` is not evidence about the edited file."""
    (tmp_path / "ast.py").write_text("def broken(:\n", encoding="utf-8")
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = check_edit_syntax(
        str(target), str(tmp_path), executor=_real_local_executor
    )

    assert result["verdict"] == "ok", result


def test_hostile_string_subclass_from_executor_degrades_unavailable(tmp_path):
    """Executor output must be plain text, not code-bearing ``str`` subclasses."""
    f = tmp_path / "hostile.py"
    f.write_text("value = 1\n", encoding="utf-8")

    class HostileText(str):
        def strip(self, *args, **kwargs):
            raise RuntimeError("must not execute subclass methods")

    result = check_edit_syntax(
        str(f), str(tmp_path),
        executor=_fake_executor(1, "", HostileText("SyntaxError: invalid syntax")),
    )

    assert result["verdict"] == "unavailable", result
    assert result["reason"] == "spawn_error", result


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


@pytest.mark.parametrize("invalid_rc", [False, 0.0, "0"])
def test_executor_returncode_contract_is_strict(tmp_path, invalid_rc):
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")

    res = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(invalid_rc, "", "")
    )

    assert res["verdict"] == "unavailable", res
    assert res["reason"] == "spawn_error", res


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [([], ""), ("", {}), (b"SyntaxError: invalid syntax", "")],
)
def test_executor_non_string_output_contract_degrades_unavailable(
    tmp_path, stdout, stderr
):
    f = tmp_path / "x.js"
    f.write_text("const x = 1;\n")

    res = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(1, stdout, stderr)
    )

    assert res["verdict"] == "unavailable", res
    assert res["reason"] == "spawn_error", res


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
