"""Pytest suite for L5 pre-finish gate.

Targets ``tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py``.

Each test builds a self-contained synthetic git repo + ``graph.db`` under a
temporary directory, then invokes the gate's ``main`` function (loaded via
``importlib`` because the script lives outside the ``groundtruth`` package).
Verdicts are read back from ``$GT_INSTANCE_LOG_DIR/gt_pre_finish_gate.json``.

Anti-benchmaxxing: the synthetic repo uses a generic ``pkg.util.parse_url``
function and three generic call sites — no SWE-bench-Live / Live-Lite /
benchmark-specific names anywhere.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Iterator

import pytest

# ── Locate and import the gate module under test ─────────────────────────────
GATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools" / "sweagent" / "gt_pre_finish_gate" / "lib" / "gt_pre_finish_gate.py"
)


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("_gt_pre_finish_gate_under_test", GATE_PATH)
    assert spec is not None and spec.loader is not None, f"Cannot load {GATE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load_gate_module()


# ── Helpers ──────────────────────────────────────────────────────────────────
def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        capture_output=True, text=True,
    )


def _init_repo(repo: Path) -> None:
    """Create a tracked baseline:

      pkg/__init__.py
      pkg/util.py            (def parse_url(s):)
      callers/a.py           (uses parse_url)
      callers/b.py           (uses parse_url)
      callers/c.py           (uses parse_url)
      tests/test_util.py     (a placeholder real test)
      README.md
    """
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    # Local identity so commits succeed without falling back to global.
    _run_git(repo, "config", "user.email", "l5-test@example.invalid")
    _run_git(repo, "config", "user.name", "L5 Test")
    _run_git(repo, "config", "commit.gpgsign", "false")

    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s):\n"
        "    return s.strip()\n"
    )

    (repo / "callers").mkdir()
    (repo / "callers" / "a.py").write_text(
        "from pkg.util import parse_url\n"
        "def a():\n"
        "    return parse_url('a')\n"
    )
    (repo / "callers" / "b.py").write_text(
        "from pkg.util import parse_url\n"
        "def b():\n"
        "    return parse_url('b')\n"
    )
    (repo / "callers" / "c.py").write_text(
        "from pkg.util import parse_url\n"
        "def c():\n"
        "    return parse_url('c')\n"
    )

    (repo / "tests").mkdir()
    (repo / "tests" / "test_util.py").write_text(
        "from pkg.util import parse_url\n"
        "def test_parse_url():\n"
        "    assert parse_url(' x ') == 'x'\n"
    )

    (repo / "README.md").write_text("# fixture repo\n")

    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "baseline")


def _build_graph_db(db_path: Path) -> None:
    """Create a graph.db consistent with what the indexer would produce.

    Schema mirrors ``D:/Groundtruth/graph.db``: ``nodes`` + ``edges``. We
    populate:

      nodes: parse_url (Function in pkg/util.py), a/b/c (Functions in
             callers/a.py, etc), and a top-level ``pkg`` Module node so
             the local-import heuristic has files under ``pkg/``.
      edges: 3 CALLS edges from a/b/c → parse_url with resolution_method =
             'import' and confidence 1.0  (sufficient for the gate's
             VERIFIED + confidence>=0.7 filter).

    Note: we deliberately do NOT add a node for ``nonexistent_local_module``
    so the hallucinated-import positive test sees a missing node despite
    the path heuristic matching.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES nodes(id),
            target_id INTEGER NOT NULL REFERENCES nodes(id),
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
    """)

    def add_node(label, name, file_path, qualified=None):
        cur = conn.execute(
            "INSERT INTO nodes(label,name,qualified_name,file_path,language) "
            "VALUES (?,?,?,?,?)",
            (label, name, qualified or name, file_path, "python"),
        )
        return cur.lastrowid

    util_id = add_node("Function", "parse_url", "pkg/util.py", "pkg.util.parse_url")
    a_id = add_node("Function", "a", "callers/a.py", "callers.a.a")
    b_id = add_node("Function", "b", "callers/b.py", "callers.b.b")
    c_id = add_node("Function", "c", "callers/c.py", "callers.c.c")
    # File-path nodes so _looks_local sees both pkg/ and callers/ tops.
    add_node("Module", "util", "pkg/util.py", "pkg.util")
    add_node("Module", "a", "callers/a.py", "callers.a")

    for src, src_file in ((a_id, "callers/a.py"), (b_id, "callers/b.py"),
                          (c_id, "callers/c.py")):
        conn.execute(
            "INSERT INTO edges(source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES (?,?,?,?,?,?,?)",
            (src, util_id, "CALLS", 3, src_file, "import", 1.0),
        )

    conn.commit()
    conn.close()


@contextmanager
def _gate_env(repo: Path, log_dir: Path, db_path: Path | None) -> Iterator[None]:
    """Set env vars expected by the gate; restore on exit."""
    saved = {k: os.environ.get(k) for k in
             ("GT_GATE_CWD", "GT_INSTANCE_LOG_DIR", "GT_GRAPH_DB", "ROOT")}
    os.environ["GT_GATE_CWD"] = str(repo)
    os.environ["GT_INSTANCE_LOG_DIR"] = str(log_dir)
    if db_path is not None:
        os.environ["GT_GRAPH_DB"] = str(db_path)
    else:
        os.environ.pop("GT_GRAPH_DB", None)
    # Make sure $ROOT doesn't override our cwd choice.
    os.environ.pop("ROOT", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _invoke_gate(*argv_extra: str) -> tuple[int, str]:
    """Call the gate's main() and capture stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = GATE.main(["gt_pre_finish_gate", *argv_extra])
    return rc, buf.getvalue()


def _read_verdict(log_dir: Path) -> dict:
    p = log_dir / "gt_pre_finish_gate.json"
    return json.loads(p.read_text())


# ── Per-test fixture: fresh repo + graph.db ──────────────────────────────────
@pytest.fixture
def repo_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (repo, log_dir, db_path), all freshly populated."""
    repo = tmp_path / "repo"
    log_dir = tmp_path / "logs"
    db_path = tmp_path / "graph.db"
    log_dir.mkdir()
    _init_repo(repo)
    _build_graph_db(db_path)
    return repo, log_dir, db_path


# ────────────────────────────────────────────────────────────────────────────
# 1. HALLUCINATED-IMPORT
# ────────────────────────────────────────────────────────────────────────────
def test_hallucinated_import_positive(repo_fixture):
    repo, log_dir, db = repo_fixture
    # 'pkg' is a top-level path component in graph.db files (pkg/util.py),
    # so the heuristic flags 'pkg.does_not_exist' as a local claim. The
    # imported NAME 'foo' has no node → flagged.
    target = repo / "pkg" / "util.py"
    target.write_text(
        "from pkg.does_not_exist import foo\n"
        "def parse_url(s):\n"
        "    return s.strip()\n"
    )
    with _gate_env(repo, log_dir, db):
        rc, _ = _invoke_gate()
    assert rc == 0
    verdict = _read_verdict(log_dir)
    flags = verdict["checks"]["hallucinated_imports"]
    assert flags, f"Expected hallucinated-import flag; got verdict={verdict}"
    assert flags[0]["unresolved"] == "foo"
    assert verdict["result"] == "blocked"


def test_hallucinated_import_negative(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Stdlib import — top component 'os' does NOT match any path in graph.db.
    target = repo / "pkg" / "util.py"
    target.write_text(
        "from os import path\n"
        "def parse_url(s):\n"
        "    return path.normpath(s.strip())\n"
    )
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    assert verdict["checks"]["hallucinated_imports"] == [], (
        f"Stdlib import should not be flagged; got {verdict}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 2. CALLER-BLIND-EDIT
# ────────────────────────────────────────────────────────────────────────────
def test_caller_blind_edit_positive(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Modify parse_url body, no test file edited.
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s):\n"
        "    return s.strip().lower()\n"  # body changed
    )
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    cb = verdict["checks"]["caller_blind_edits"]
    assert cb, f"Expected caller-blind flag; got {verdict}"
    syms = {f["symbol"] for f in cb}
    assert "parse_url" in syms
    assert next(f for f in cb if f["symbol"] == "parse_url")["callers"] >= 3


def test_caller_blind_edit_negative(repo_fixture):
    repo, log_dir, db = repo_fixture
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s):\n"
        "    return s.strip().lower()\n"
    )
    # Also touch the test file → caller-blind is satisfied.
    (repo / "tests" / "test_util.py").write_text(
        "from pkg.util import parse_url\n"
        "def test_parse_url():\n"
        "    assert parse_url(' X ') == 'x'\n"
    )
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    assert verdict["checks"]["caller_blind_edits"] == [], (
        f"Test edited → caller-blind should be empty; got {verdict}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 3. CONTRACT-BREAK
# ────────────────────────────────────────────────────────────────────────────
def test_contract_break_positive(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Add a parameter — signature change.
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s, validate=True):\n"
        "    return s.strip()\n"
    )
    # Edit the test too so caller-blind doesn't piggyback (we're isolating
    # the contract-break check).
    (repo / "tests" / "test_util.py").write_text(
        "from pkg.util import parse_url\n"
        "def test_parse_url():\n"
        "    assert parse_url(' x ') == 'x'\n"
    )
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    cb = verdict["checks"]["contract_breaks"]
    assert cb, f"Expected contract-break flag; got {verdict}"
    assert any(f["symbol"] == "parse_url" for f in cb)


def test_contract_break_negative(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Body change only, signature unchanged.
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s):\n"
        "    s = s.strip()\n"
        "    return s\n"
    )
    # Touch tests so caller-blind doesn't fire either.
    (repo / "tests" / "test_util.py").write_text(
        "from pkg.util import parse_url\n"
        "def test_parse_url():\n"
        "    assert parse_url(' x ') == 'x'\n"
        "    assert parse_url('y') == 'y'\n"
    )
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    assert verdict["checks"]["contract_breaks"] == [], (
        f"Body-only change must not flag contract-break; got {verdict}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 4. SCRATCH-FILE
# ────────────────────────────────────────────────────────────────────────────
def test_scratch_file_untracked(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Untracked top-level scratch. RC-01: default scratch patterns are the
    # language-level set (``tmp_``, ``repro``, ``scratch_``, …); the
    # agent-fingerprint set (``test_``, ``debug_``, …) is opt-in only, so
    # the test uses a default-matching prefix here.
    (repo / "repro_issue.py").write_text("print('repro')\n")
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    sf = verdict["checks"]["scratch_files"]
    assert sf, f"Untracked scratch must be flagged; got {verdict}"
    assert any(f["file"] == "repro_issue.py" for f in sf)


def test_scratch_file_staged(repo_fixture):
    repo, log_dir, db = repo_fixture
    (repo / "reproduce_issue.py").write_text("print('repro')\n")
    _run_git(repo, "add", "reproduce_issue.py")
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    sf = verdict["checks"]["scratch_files"]
    assert any(f["file"] == "reproduce_issue.py" for f in sf), (
        f"Staged scratch must be flagged; got {verdict}"
    )


def test_scratch_file_nested_excluded(repo_fixture):
    repo, log_dir, db = repo_fixture
    # tests/ is nested, and the basename matches the prefix pattern, but the
    # path contains '/', so the gate excludes it.
    (repo / "tests" / "test_real.py").write_text(
        "def test_real():\n    assert True\n"
    )
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    assert verdict["checks"]["scratch_files"] == [], (
        f"Nested test file must not be flagged; got {verdict}"
    )


def test_scratch_file_legit_extension_ignored(repo_fixture):
    repo, log_dir, db = repo_fixture
    (repo / "README.md").write_text("# fixture repo\n\nUpdated docs.\n")
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    verdict = _read_verdict(log_dir)
    assert verdict["checks"]["scratch_files"] == [], (
        f"Non-.py modify must not be flagged; got {verdict}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 5. SOFT-ESCAPE + STRIP
# ────────────────────────────────────────────────────────────────────────────
def test_soft_escape_after_3(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Force a flaggable diff (signature change).
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s, validate=True):\n"
        "    return s.strip()\n"
    )
    # Pre-seed 3 prior blocks so this is the 4th attempt.
    (log_dir / "gt_finish_attempts.json").write_text(
        json.dumps({"blocks": 3, "ts": 0.0})
    )
    with _gate_env(repo, log_dir, db):
        rc, stdout = _invoke_gate()
    assert rc == 0
    verdict = _read_verdict(log_dir)
    assert verdict["result"] == "warn_soft_escape", (
        f"4th attempt should soft-escape; got {verdict}"
    )
    assert verdict["soft_escape"] is True
    assert verdict["submit_blocked"] is False
    # Submission markers must appear on the success path.
    assert GATE.SUBMISSION_MARKER in stdout, (
        f"Soft-escape must emit submission markers; stdout={stdout!r}"
    )


def test_strip_scratch_on_soft_escape(repo_fixture):
    repo, log_dir, db = repo_fixture
    scratch = repo / "reproduce_issue.py"
    scratch.write_text("print('repro')\n")
    _run_git(repo, "add", "reproduce_issue.py")
    # Confirm staged before we run.
    staged_before = _run_git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "reproduce_issue.py" in staged_before
    # Pre-seed 3 prior blocks → next call is the 4th.
    (log_dir / "gt_finish_attempts.json").write_text(
        json.dumps({"blocks": 3, "ts": 0.0})
    )
    with _gate_env(repo, log_dir, db):
        rc, stdout = _invoke_gate()
    assert rc == 0
    verdict = _read_verdict(log_dir)
    assert verdict["result"] == "warn_soft_escape"
    # Scratch must be in scratch_stripped list on the verdict.
    assert verdict.get("scratch_stripped") == ["reproduce_issue.py"], (
        f"scratch_stripped wrong: {verdict.get('scratch_stripped')}"
    )
    # Index no longer contains the scratch (git rm --cached worked).
    ls_files = _run_git(repo, "ls-files", "--", "reproduce_issue.py").stdout.strip()
    assert ls_files == "", (
        f"Scratch should be unstaged; ls-files={ls_files!r}"
    )
    # On-disk file remains.
    assert scratch.exists(), "git rm --cached must not delete on-disk file"
    # Emitted patch (between submission markers) must NOT contain the scratch.
    parts = stdout.split(GATE.SUBMISSION_MARKER)
    assert len(parts) >= 3, f"expected two markers; stdout={stdout!r}"
    patch_body = parts[1]
    assert "reproduce_issue.py" not in patch_body, (
        f"emitted patch must exclude scratch; body={patch_body!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 6. VERDICT FILE SHAPE
# ────────────────────────────────────────────────────────────────────────────
def test_verdict_writes_result_key(repo_fixture):
    repo, log_dir, db = repo_fixture
    with _gate_env(repo, log_dir, db):
        _invoke_gate()
    raw = (log_dir / "gt_pre_finish_gate.json").read_text()
    parsed = json.loads(raw)
    assert "result" in parsed, f"verdict must use 'result' key; got keys={list(parsed)}"
    assert "verdict" not in parsed, (
        "verdict file must NOT use legacy 'verdict' key — that bug was fixed"
    )


# ────────────────────────────────────────────────────────────────────────────
# 7. NO GRAPH DB → SOFT PASS
# ────────────────────────────────────────────────────────────────────────────
def test_no_graph_db_pass(repo_fixture):
    repo, log_dir, _db = repo_fixture
    # Make sure SOMETHING is in the diff so the early "no edited files" branch
    # doesn't short-circuit before the graph-db check.
    (repo / "pkg" / "util.py").write_text(
        "def parse_url(s):\n    return s.strip()  # comment\n"
    )
    with _gate_env(repo, log_dir, db_path=None):
        rc, stdout = _invoke_gate()
    assert rc == 0
    verdict = _read_verdict(log_dir)
    assert verdict["result"] == "no_graph_db", (
        f"Missing GT_GRAPH_DB must soft-pass with result=no_graph_db; got {verdict}"
    )
    assert GATE.SUBMISSION_MARKER in stdout


# ────────────────────────────────────────────────────────────────────────────
# 8. FORCE FLAG BYPASS
# ────────────────────────────────────────────────────────────────────────────
def test_force_flag_bypass(repo_fixture):
    repo, log_dir, db = repo_fixture
    # Stage a diff that WOULD flag every check, just to make the bypass
    # meaningful.
    (repo / "pkg" / "util.py").write_text(
        "from pkg.does_not_exist import foo\n"
        "def parse_url(s, validate=True):\n"
        "    return s.strip()\n"
    )
    (repo / "debug_test.py").write_text("print('repro')\n")
    with _gate_env(repo, log_dir, db):
        rc, stdout = _invoke_gate("-f")
    assert rc == 0
    verdict = _read_verdict(log_dir)
    assert verdict["result"] == "force", f"--force must short-circuit; got {verdict}"
    # No checks should have run on the force path.
    assert verdict["checks"]["hallucinated_imports"] == []
    assert verdict["checks"]["caller_blind_edits"] == []
    assert verdict["checks"]["contract_breaks"] == []
    assert verdict["checks"]["scratch_files"] == []
    assert GATE.SUBMISSION_MARKER in stdout


# ── RC-09: _changed_symbols precision ────────────────────────────────────────
def _make_n_function_file(n: int) -> str:
    """Return text for a Python file containing ``n`` distinct functions."""
    lines: list[str] = []
    for i in range(n):
        lines.append(f"def func_{i}(x):")
        lines.append(f"    return x + {i}")
        lines.append("")
    return "\n".join(lines)


def test_rc09_changed_symbols_one_line_edit_returns_exactly_one():
    """RC-09: A 1-line body edit to one of 10 functions must yield exactly
    that function's name — not all 10."""
    before = _make_n_function_file(10)
    # Mutate only func_3's body line.
    after = before.replace("    return x + 3", "    return x + 30", 1)
    assert before != after
    changed = GATE._changed_symbols(before, after)
    assert changed == {"func_3"}, (
        f"Expected exactly {{'func_3'}}; got {changed} "
        f"(over-flagging would defeat the caller-blind soft-escape)."
    )


def test_rc09_changed_symbols_no_diff_returns_empty():
    text = _make_n_function_file(5)
    assert GATE._changed_symbols(text, text) == set()


def test_rc09_changed_symbols_added_symbol_returned():
    before = _make_n_function_file(3)
    after = before + "\ndef func_new(y):\n    return y\n"
    changed = GATE._changed_symbols(before, after)
    assert "func_new" in changed
    # No spurious flags on the unchanged 3 originals.
    assert changed == {"func_new"}


def test_rc09_changed_symbols_for_lang_python_one_function():
    """Per-language path mirrors the Python-only path."""
    before = _make_n_function_file(10)
    after = before.replace("    return x + 7", "    return x + 70", 1)
    changed = GATE._changed_symbols_for_lang(before, after, "python")
    assert changed == {"func_7"}


def test_rc09_emit_submission_aborts_on_corrupted_test_patch(
    repo_fixture, monkeypatch, capsys,
):
    """RC-09: when ``/root/test.patch`` exists but reverse-apply fails,
    ``emit_submission`` must abort with a clear stderr message rather than
    silently shipping a contaminated diff."""
    repo, _log_dir, _db = repo_fixture
    # Stage a fake "/root/test.patch" by redirecting Path("/root/test.patch")
    # to a fixture file in the repo. We patch GATE.Path so emit_submission's
    # is_file()/stat()/read pass while git apply -R obviously fails on a
    # nonsense patch body.
    fake_patch = repo / "_fake_test.patch"
    fake_patch.write_text("not a real diff\n", encoding="utf-8")

    real_path = GATE.Path

    class _Reroute(type(real_path)):
        pass  # unused — we route via a wrapper instead.

    def _path_proxy(arg):
        if isinstance(arg, str) and arg == "/root/test.patch":
            return fake_patch
        if isinstance(arg, str) and arg == "/root/model.patch":
            return repo / "_fake_model.patch"
        return real_path(arg)

    monkeypatch.setattr(GATE, "Path", _path_proxy)
    GATE.emit_submission(str(repo))
    out = capsys.readouterr()
    assert GATE.SUBMISSION_MARKER not in out.out, (
        "Submission must NOT be emitted when reverse-apply fails."
    )
    assert "ABORT" in out.err
    assert "test.patch" in out.err


def test_rc09_jinja_sanitize_round_trip():
    """RC-09: brief content with literal ``{{ }}`` / ``{% %}`` substrings
    must be neutralised before injection so a downstream Jinja renderer
    cannot re-tokenise them. We verify the sanitiser used inline in
    gt_edit_state by reproducing its replacement scheme."""
    sample = "see {{ user_input }} and {% block x %}body{% endblock %}"
    zwnj = "‌"
    sanitised = sample
    for needle in ("{{", "}}", "{%", "%}"):
        sanitised = sanitised.replace(needle, needle[0] + zwnj + needle[1])
    assert "{{" not in sanitised
    assert "}}" not in sanitised
    assert "{%" not in sanitised
    assert "%}" not in sanitised
    # Visually identical when zero-width-non-joiner is rendered.
    assert sanitised.replace(zwnj, "") == sample
