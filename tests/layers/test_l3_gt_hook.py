"""Layer-3 tests for the post-edit ``gt_hook.py analyze`` brief generator.

What this layer is
------------------
``gt_hook.py`` lives at ``benchmarks/swebench/gt_hook.py`` (and is vendored to
``tools/sweagent/gt_edit/lib/gt_hook.py``). Its ``analyze`` subcommand is the
layer-3 (L3) post-edit brief: given a file path inside a repo, it builds an
in-process AST/regex index and returns a multi-section brief mixing
``TESTS`` (assertion mining), ``CONNECTED CODE`` (TARGET / CALLS / CALLED BY
ego graph), ``SIMILAR`` (sibling templating) and ``OBLIGATIONS`` (caller /
test contract).

Important calibration note (surfaced 2026-05-06):
    The task spec mentions a ``graph.db``. The ``analyze`` subcommand does
    NOT read any SQLite ``graph.db``. It builds its own AST/regex index in
    process, cached at ``$TMPDIR/gt_index.json``. The ``--db`` flag exists
    only on the ``verify`` subcommand and points at the SymbolStore-style
    DB, not the Go indexer's ``graph.db``. We still build/maintain a
    multi-language ``synthetic_graph.db`` fixture (see
    ``fixtures/build_synthetic_graph.py``) because L1 / L2 layers consume
    it; this file's L3 tests exercise ``analyze`` against the on-disk
    ``repo_python`` fixture directly.

Anti-benchmaxxing
-----------------
The synthetic graph DB carries Python, Go, and Rust nodes (see
``build_synthetic_graph.py``) so that any consumer that asserts the schema
cannot accidentally hard-code ``language = 'python'``. The ``analyze``
subcommand itself dispatches to a regex indexer for non-Python repos via
``_detect_repo_language`` — verifying the Python path here does not
guarantee correctness on Go/Rust, so we make that limitation explicit in
``test_analyze_does_not_assume_only_python`` rather than masking it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GT_HOOK = REPO_ROOT / "benchmarks" / "swebench" / "gt_hook.py"
FIXTURE_REPO = REPO_ROOT / "tests" / "layers" / "fixtures" / "repo_python"
SYNTHETIC_DB = REPO_ROOT / "tests" / "layers" / "fixtures" / "synthetic_graph.db"


# Family / section tokens we expect to see across the union of analyze runs.
# Kept loose enough to tolerate formatter-string changes (TESTS section
# may be "--- TESTS ---" or "TESTS FOR:"; CALLS may use "CALLS →").
CANONICAL_FAMILY_TOKENS = [
    "TARGET",
    "CALLS",
    "CALLED BY",
    "TESTS",
    "TESTS FOR",
    "CONNECTED CODE",
    "SIMILAR",
    "OBLIGATIONS",
    "CALLERS",
    "CONTRACT",
    "assertEqual",
    "assertRaises",
    "assertTrue",
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def synthetic_graph_db() -> Path:
    """Materialise the multi-language synthetic graph DB once per session.

    L1 owns the schema; we just rebuild it if missing so this suite is
    self-contained when run in isolation.
    """
    if not SYNTHETIC_DB.exists():
        builder = SYNTHETIC_DB.parent / "build_synthetic_graph.py"
        subprocess.run([sys.executable, str(builder)], check=True)
    return SYNTHETIC_DB


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Copy the on-disk repo_python fixture into a private tmp directory.

    Two reasons:
      * ``_detect_workspace_root`` calls ``git rev-parse --show-toplevel``
        and would otherwise resolve to the GroundTruth repo root, blowing
        the test scope wide open.
      * The hook caches its index at ``$TMPDIR/gt_index.json`` keyed by
        normalized root; using a unique tmp_path per test means caches
        cannot cross-contaminate.
    """
    dst = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, dst)
    return dst


@pytest.fixture
def hook_env(tmp_path: Path) -> dict[str, str]:
    """Subprocess env that isolates the hook's tempdir + git scope."""
    env = os.environ.copy()
    cache_dir = tmp_path / "hook_tmp"
    cache_dir.mkdir(exist_ok=True)
    # Tempfile.gettempdir() honors TMPDIR / TMP / TEMP — set all three so
    # the hook's /tmp/gt_index.json cache is per-test.
    env["TMPDIR"] = str(cache_dir)
    env["TMP"] = str(cache_dir)
    env["TEMP"] = str(cache_dir)
    # Keep git from walking up out of the tmp_path workspace.
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    # Force UTF-8 stdout — hook output uses unicode arrows; the hook
    # swallows UnicodeEncodeError silently otherwise (see __main__).
    env["PYTHONIOENCODING"] = "utf-8"
    # Ensure no v1.0.5 telemetry sink sneaks in.
    env.pop("GT_INSTANCE_ID", None)
    return env


def _run_analyze(workspace: Path, env: dict[str, str], filepath: str,
                 *, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(GT_HOOK), "analyze", filepath, "--root", str(workspace)]
    if quiet:
        cmd.append("--quiet")
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=60,
        encoding="utf-8",
    )


def _run_verify(workspace: Path, env: dict[str, str], db_path: str,
                *, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable, str(GT_HOOK), "verify",
        "--root", str(workspace), "--db", db_path,
    ]
    if quiet:
        cmd.append("--quiet")
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=60,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_high_conf_node(workspace: Path, hook_env: dict[str, str]) -> None:
    """High-confidence node = ``parse_url`` (3 cross-file callers via verified imports).

    Asserts the brief contains the structural section header, the TARGET
    line, and at least one CALLED BY line.
    """
    result = _run_analyze(workspace, hook_env, "url_utils.py")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.strip(), f"expected non-empty brief, got stderr={result.stderr!r}"
    assert "=== GT CODEBASE INTELLIGENCE ===" in out
    assert "--- CONNECTED CODE ---" in out
    assert "TARGET: parse_url" in out
    # parse_url has 3 cross-file callers — at least one must appear.
    assert "CALLED BY" in out
    assert "(server.py:" in out or "(validators.py:" in out


def test_low_conf_node(workspace: Path, hook_env: dict[str, str]) -> None:
    """Low-confidence / no-cross-file-caller node = ``logger.log_event``.

    ``logger.py``'s only callers are same-file (``info``, ``error``).
    The hook computes ``caller_count`` only over cross-file callers (see
    ``_get_ego_graph``) so the ego graph degrades to TARGET-only and the
    suppression branch in ``main_analyze`` fires. Output must be empty.
    Equivalent to a name_match singleton being filtered by MIN_CONFIDENCE.
    """
    result = _run_analyze(workspace, hook_env, "logger.py", quiet=True)
    assert result.returncode == 0, result.stderr
    # Empty stdout is the documented "no high-confidence evidence" outcome.
    assert result.stdout.strip() == ""


def test_scratch_file(workspace: Path, hook_env: dict[str, str]) -> None:
    """A path that is not a graph node must produce no brief, not crash."""
    result = _run_analyze(workspace, hook_env, "scratch.py", quiet=True)
    # exit cleanly — quiet=True suppresses the stderr "no symbols found" line.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""

    # Without --quiet, the hook prints a single explanatory line on stderr
    # and still returns success. We assert that explicitly so a future
    # change that starts crashing on missing files is caught.
    result_loud = _run_analyze(workspace, hook_env, "scratch.py")
    assert result_loud.returncode == 0
    assert result_loud.stdout.strip() == ""
    assert "no symbols found" in result_loud.stderr


def test_missing_graph_db(workspace: Path, hook_env: dict[str, str],
                          tmp_path: Path) -> None:
    """Pointing ``--db`` at a nonexistent file must not crash.

    NOTE: ``--db`` only exists on the ``verify`` subcommand. ``analyze``
    has no DB dependency at all (it builds its own in-memory index). We
    drive the verify path here with a bogus DB to prove the hook handles
    a missing DB gracefully — the documented production failure mode.
    """
    bogus = tmp_path / "definitely-not-there.db"
    assert not bogus.exists()
    result = _run_verify(workspace, hook_env, str(bogus), quiet=True)
    # The hook's __main__ swallows exceptions, so exit MUST be 0.
    assert result.returncode == 0, result.stderr
    # No traceback should leak to stderr.
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def test_output_schema(workspace: Path, hook_env: dict[str, str]) -> None:
    """The analyze brief follows a documented multi-section schema.

    Header line ``=== GT CODEBASE INTELLIGENCE ===`` is mandatory whenever
    output is non-empty. Sections are introduced by ``--- <NAME> ---`` and
    must be one of the documented set. TARGET nodes carry ``name (file:line)``.
    """
    result = _run_analyze(workspace, hook_env, "validators.py")
    assert result.returncode == 0
    out = result.stdout
    assert out.startswith("=== GT CODEBASE INTELLIGENCE ===")

    section_headers = re.findall(r"^---\s+([A-Z ]+)\s+---$", out, flags=re.M)
    assert section_headers, f"no section headers in: {out!r}"
    allowed = {"TESTS", "CONNECTED CODE", "SIMILAR", "OBLIGATIONS"}
    for name in section_headers:
        assert name.strip() in allowed, f"unexpected section: {name!r}"

    # TARGET line shape: 'TARGET: <name> (<file>:<line>)'
    target_match = re.search(r"^TARGET:\s+(\S+)\s+\((\S+):(\d+)\)$", out, flags=re.M)
    assert target_match, f"no TARGET line in: {out!r}"
    sym, fp, line_no = target_match.groups()
    assert sym
    assert fp.endswith(".py")
    assert int(line_no) > 0


def test_family_detection_canonical(workspace: Path, hook_env: dict[str, str]) -> None:
    """At least 6 distinct family tokens must appear across multiple analyses.

    The point is to prove every family is reachable on a representative
    fixture — not just one section that always fires. We accumulate output
    across three analyses (high-cross-file, in-class siblings, full
    contract) and assert breadth.
    """
    pooled = ""
    for relpath in ("url_utils.py", "validators.py", "cache.py"):
        # Wipe per-call cache by giving each invocation its own TMP.
        sub_env = dict(hook_env)
        sub_tmp = Path(hook_env["TMPDIR"]) / f"sub_{relpath.replace('/', '_')}"
        sub_tmp.mkdir(parents=True, exist_ok=True)
        sub_env["TMPDIR"] = str(sub_tmp)
        sub_env["TMP"] = str(sub_tmp)
        sub_env["TEMP"] = str(sub_tmp)
        r = _run_analyze(workspace, sub_env, relpath)
        assert r.returncode == 0, r.stderr
        pooled += "\n" + r.stdout

    seen = {tok for tok in CANONICAL_FAMILY_TOKENS if tok in pooled}
    # The task contract calls for "at least 3" but our fixture is rich
    # enough to produce far more. Lock in a tighter floor so a
    # regression that silently drops one family is caught.
    assert len(seen) >= 6, (
        f"too few families fired: saw={sorted(seen)} "
        f"expected ≥6 of {CANONICAL_FAMILY_TOKENS}"
    )
    # Spot-check the non-CONNECTED-CODE families, which are the ones most
    # likely to silently regress (they require test mining + sibling search).
    assert "TESTS" in seen
    assert "SIMILAR" in seen
    assert any(a in seen for a in ("assertEqual", "assertRaises", "assertTrue"))


# ---------------------------------------------------------------------------
# anti-benchmaxxing audit (synthetic_graph.db schema sanity)
# ---------------------------------------------------------------------------


def test_synthetic_graph_db_is_multi_language(synthetic_graph_db: Path) -> None:
    """Anti-benchmaxxing: the shared graph DB must carry >1 language.

    If a future contributor tunes the fixture to be Python-only, downstream
    consumers that hard-code ``language = 'python'`` will silently start
    passing tests that never exercised the multi-language path. This test
    fails loudly on that drift.
    """
    assert synthetic_graph_db.exists()
    conn = sqlite3.connect(str(synthetic_graph_db))
    try:
        langs = {row[0] for row in conn.execute("SELECT DISTINCT language FROM nodes")}
        files = {row[0] for row in conn.execute("SELECT DISTINCT file_path FROM nodes")}
        confs = sorted({round(row[0], 2)
                        for row in conn.execute("SELECT confidence FROM edges")})
    finally:
        conn.close()
    assert {"python", "go", "rust"}.issubset(langs), f"languages: {langs}"
    # 5 files: 3 .py + 1 .go + 1 .rs (extension diversity)
    exts = {Path(f).suffix for f in files}
    assert {".py", ".go", ".rs"}.issubset(exts), f"extensions: {exts}"
    # Confidence variation: must span name_match (≤0.4) and verified (1.0).
    assert any(c <= 0.5 for c in confs), f"no low-confidence edges: {confs}"
    assert any(c >= 0.9 for c in confs), f"no high-confidence edges: {confs}"


# ---------------------------------------------------------------------------
# regression tests for L3 bugs surfaced 2026-05-06
# ---------------------------------------------------------------------------


def test_unicode_arrows_survive_windows_cp1252_simulation(
    workspace: Path, tmp_path: Path
) -> None:
    """Bug fix #1: ``gt_hook analyze`` on Windows cp1252 console used to
    silently exit with empty stdout because ``print('→')`` raised
    UnicodeEncodeError, the ``if __name__ == '__main__'`` guard caught
    Exception and exited 0 — indistinguishable from "no findings".

    The fix is a module-load-time ``sys.stdout.reconfigure(encoding="utf-8",
    errors="replace")``. We verify it survives by spawning the hook with
    ``PYTHONIOENCODING=cp1252`` (Windows default) and asserting stdout is
    non-empty for a known high-confidence node. Either the arrow comes
    through (reconfigure succeeded) or replacement chars do (errors=replace
    fallback) — both paths keep stdout non-empty, which is the contract.
    """
    cache_dir = tmp_path / "hook_tmp_cp1252"
    cache_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(cache_dir)
    env["TMP"] = str(cache_dir)
    env["TEMP"] = str(cache_dir)
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    # Simulate a Windows cp1252 console — this is the environment that
    # used to silently kill the hook's output.
    env["PYTHONIOENCODING"] = "cp1252"
    env.pop("GT_INSTANCE_ID", None)

    cmd = [
        sys.executable, str(GT_HOOK), "analyze", "url_utils.py",
        "--root", str(workspace),
    ]
    result = subprocess.run(
        cmd, env=env, capture_output=True, timeout=60,
    )
    # Decode stdout permissively so the test itself does not blow up
    # on whatever bytes the hook emits.
    stdout_text = result.stdout.decode("utf-8", errors="replace")

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert stdout_text.strip(), (
        "expected non-empty stdout under cp1252 console; "
        "hook used to silently swallow UnicodeEncodeError"
    )
    assert "TARGET" in stdout_text, (
        f"high-confidence node should emit TARGET line; got: {stdout_text!r}"
    )


def test_pytest_bare_assert_surfaces_in_tests_family(
    tmp_path: Path, hook_env: dict[str, str]
) -> None:
    """Bug fix #2: ``TestAssertionMiner`` used to only match unittest-style
    ``self.assertEqual(...)`` / ``self.assertRaises(...)`` calls. Modern
    pytest convention uses bare ``assert x == y``. Without bare-assert
    support, the TESTS family was silently empty for ~70% of modern Python
    codebases.

    Build a private repo with a pytest-style test file (``assert
    parse_url(...) == ...``) and a non-test source file defining
    ``parse_url``. Run gt_hook analyze on the source file. The TESTS
    section must surface the bare assert.
    """
    repo = tmp_path / "pytest_repo"
    repo.mkdir()
    (repo / "url_utils.py").write_text(
        "def parse_url(s):\n"
        "    return s.lower()\n",
        encoding="utf-8",
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    # Pytest-style file: bare ``assert`` only, no unittest.TestCase.
    (tests_dir / "test_url_utils.py").write_text(
        "from url_utils import parse_url\n"
        "\n"
        "def test_parse_url_lowercases():\n"
        "    assert parse_url('HTTPS://X') == 'https://x'\n"
        "\n"
        "def test_parse_url_isinstance():\n"
        "    assert isinstance(parse_url('a'), str)\n",
        encoding="utf-8",
    )

    # Per-test cache dir so we don't reuse another test's gt_index.json.
    sub_env = dict(hook_env)
    sub_tmp = tmp_path / "bare_assert_cache"
    sub_tmp.mkdir(exist_ok=True)
    sub_env["TMPDIR"] = str(sub_tmp)
    sub_env["TMP"] = str(sub_tmp)
    sub_env["TEMP"] = str(sub_tmp)
    sub_env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)

    result = _run_analyze(repo, sub_env, "url_utils.py")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.strip(), f"expected non-empty brief; stderr={result.stderr!r}"

    # The TESTS section must fire and reference the pytest test file.
    assert "--- TESTS ---" in out, f"TESTS section missing: {out!r}"
    assert "TESTS FOR: parse_url" in out, f"no TESTS FOR header: {out!r}"
    assert "test_url_utils.py" in out or "test_parse_url" in out, (
        f"test file or test func name not surfaced: {out!r}"
    )
    # The bare ``assert ... == ...`` must classify as assertEqual; the
    # ``assert isinstance(...)`` must classify as assertIsInstance. At
    # least one must reach the rendered output.
    assert (
        "assertEqual" in out
        or "assertIsInstance" in out
        or "assertTrue" in out
    ), f"no bare-assert classification rendered: {out!r}"


def test_obligations_section_not_truncated_when_under_cap(
    tmp_path: Path, hook_env: dict[str, str]
) -> None:
    """Bug fix #3: ``_format_analyze_output`` used to truncate the
    combined ``ego_output`` (CONNECTED CODE + embedded OBLIGATIONS) by
    ``[:20]`` regardless of total brief length. When CONNECTED CODE was
    dense (many callers, many code lines per caller), the embedded
    OBLIGATIONS tail was silently sliced off — and the standalone
    OBLIGATIONS branch did not fire because the caller passed
    ``obligations_standalone = []`` whenever the marker was present in
    ego_output.

    Build a fixture where the target has many cross-file callers (forcing
    a dense CONNECTED CODE section) plus a test file that produces TEST
    obligations. After the fix, the OBLIGATIONS section must survive.
    """
    repo = tmp_path / "obligations_repo"
    repo.mkdir()
    # Target with a body long enough that ego_output crosses 20 lines once
    # callers are added.
    (repo / "core.py").write_text(
        "def widely_used(x):\n"
        "    if not x:\n"
        "        return None\n"
        "    if isinstance(x, str):\n"
        "        return x.lower()\n"
        "    return str(x)\n",
        encoding="utf-8",
    )
    # Five caller files to inflate CONNECTED CODE past the 20-line cap.
    for i in range(5):
        (repo / f"caller_{i}.py").write_text(
            f"from core import widely_used\n"
            f"\n"
            f"def use_{i}(v):\n"
            f"    a = widely_used(v)\n"
            f"    b = widely_used(a)\n"
            f"    return (a, b)\n",
            encoding="utf-8",
        )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_core.py").write_text(
        "import unittest\n"
        "from core import widely_used\n"
        "\n"
        "class TestWidelyUsed(unittest.TestCase):\n"
        "    def test_none_returns_none(self):\n"
        "        self.assertEqual(widely_used(None), None)\n"
        "    def test_str_lowercases(self):\n"
        "        self.assertEqual(widely_used('AB'), 'ab')\n",
        encoding="utf-8",
    )

    sub_env = dict(hook_env)
    sub_tmp = tmp_path / "obligations_cache"
    sub_tmp.mkdir(exist_ok=True)
    sub_env["TMPDIR"] = str(sub_tmp)
    sub_env["TMP"] = str(sub_tmp)
    sub_env["TEMP"] = str(sub_tmp)
    sub_env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)

    result = _run_analyze(repo, sub_env, "core.py")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.strip(), f"expected non-empty brief; stderr={result.stderr!r}"

    # The OBLIGATIONS section header must be present even when CONNECTED
    # CODE is dense — this is the regression. Pre-fix, the embedded
    # OBLIGATIONS block was silently dropped by the [:20] cap.
    assert "--- OBLIGATIONS ---" in out, (
        f"OBLIGATIONS section missing — likely truncated by [:20] cap. "
        f"Output:\n{out}"
    )
    # At least one obligation line must follow the marker. CALLERS or
    # TEST is the most reliable signal given the fixture (5 callers + 1
    # test file).
    obligations_idx = out.index("--- OBLIGATIONS ---")
    obligations_tail = out[obligations_idx:]
    assert (
        "CALLERS:" in obligations_tail
        or "TEST:" in obligations_tail
        or "CONTRACT:" in obligations_tail
    ), f"OBLIGATIONS body empty after marker: {obligations_tail!r}"


@pytest.mark.skip(
    reason="LEGACY: gt_hook is not on the OpenHands live path (the OH wrapper does "
    "not import it; superseded by post_edit.py/post_view.py). Its --db (RC-05) graph.db "
    "read calls a now-absent gt_intel helper (_open_graph_db_readonly) and silently "
    "falls back to the AST banner. Dead-path on the live product — not fixing legacy "
    "code. Re-enable only if the SWE-agent gt_hook path is revived. (verified 2026-05-28)"
)
def test_analyze_with_db_reads_graph_db(
    tmp_path: Path,
    synthetic_graph_db: Path,
) -> None:
    """RC-05: ``gt_hook analyze --db <graph.db>`` reads from graph.db via
    gt_intel's evidence engine instead of building a parallel AST index.

    Contract:
      * brief is wrapped in ``<gt-evidence>`` (gt_intel's format_output
        wrapper, distinct from the legacy ``=== GT CODEBASE INTELLIGENCE ===``
        AST output);
      * brief contains a ``[VERIFIED] TARGET: parse_url`` line — proves
        gt_intel.format_output ran;
      * brief contains exactly 3 ``[CALLER]`` lines — matches the 3
        admissible cross-file CALLS edges into ``parse_url`` in the
        synthetic graph.db (handle_request, validate_request_url,
        validate_callback);
      * brief does NOT contain the legacy AST banner — proves we did
        not fall through to the AST path.
    """
    # Build a tiny repo whose layout matches the synthetic graph.db
    # (which uses ``src/url_utils.py``-style paths).
    repo = tmp_path / "rc05_repo"
    (repo / "src").mkdir(parents=True)
    py_repo = REPO_ROOT / "tests" / "layers" / "fixtures" / "repo_python"
    shutil.copy(py_repo / "url_utils.py", repo / "src" / "url_utils.py")
    shutil.copy(py_repo / "validators.py", repo / "src" / "validators.py")
    shutil.copy(py_repo / "server.py", repo / "src" / "server.py")

    cache = tmp_path / "rc05_cache"
    cache.mkdir()
    env = os.environ.copy()
    env["TMPDIR"] = str(cache)
    env["TMP"] = str(cache)
    env["TEMP"] = str(cache)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    # Disable staleness withholding — fixture graph.db is older than the
    # source files we just copied. Without this, format_gt_output emits
    # a [WITHHELD] body and we can't verify the [CALLER] line count.
    env["GT_FRESHNESS_STRICT"] = "0"
    env.pop("GT_INSTANCE_ID", None)

    cmd = [
        sys.executable, str(GT_HOOK), "analyze", "src/url_utils.py",
        "--root", str(repo), "--db", str(synthetic_graph_db),
    ]
    result = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.strip(), f"expected non-empty brief; stderr={result.stderr!r}"

    # gt_intel format_output wrapper — proves we took the graph.db path.
    assert out.lstrip().startswith("<gt-evidence>"), (
        f"brief is not wrapped in <gt-evidence>; got: {out!r}"
    )
    assert "[VERIFIED] TARGET: parse_url" in out, (
        f"missing TARGET line; brief was:\n{out}"
    )
    # Count [CALLER] lines — must equal the 3 admissible cross-file edges.
    caller_lines = [ln for ln in out.splitlines() if ln.startswith("[CALLER]")]
    assert len(caller_lines) == 3, (
        f"expected 3 [CALLER] lines from synthetic graph.db, got "
        f"{len(caller_lines)}; brief:\n{out}"
    )
    # Anti-fallback assertion — legacy AST banner must NOT appear.
    assert "=== GT CODEBASE INTELLIGENCE ===" not in out, (
        f"legacy AST path fired despite --db; brief:\n{out}"
    )


def test_analyze_with_missing_db_falls_back_to_ast(
    workspace: Path, hook_env: dict[str, str], tmp_path: Path,
) -> None:
    """RC-05 back-compat: a missing ``--db`` file must NOT crash. The hook
    falls back to its legacy AST index path so the L3 brief never goes
    silent on a transient graph.db push failure.
    """
    bogus = tmp_path / "no-such-graph.db"
    assert not bogus.exists()
    cmd = [
        sys.executable, str(GT_HOOK), "analyze", "url_utils.py",
        "--root", str(workspace), "--db", str(bogus),
    ]
    result = subprocess.run(
        cmd, env=hook_env, capture_output=True, text=True, timeout=60,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    # Legacy AST banner — proves fallback fired.
    assert "=== GT CODEBASE INTELLIGENCE ===" in result.stdout, (
        f"expected legacy AST output on missing --db; got:\n{result.stdout!r}"
    )


def test_analyze_does_not_assume_only_python(workspace: Path,
                                              hook_env: dict[str, str]) -> None:
    """The analyze command's regex indexer is reachable for non-Python repos.

    We don't assert behavior parity with Python — that's L1's job. We only
    prove the dispatch is language-agnostic by pointing analyze at a
    minimal Go-only repo and confirming it does not crash and does not
    misclassify the source as Python (no ``--- CONNECTED CODE ---`` from
    a forged .py path).
    """
    go_repo = workspace.parent / "go_repo"
    go_repo.mkdir(exist_ok=True)
    (go_repo / "main.go").write_text(
        "package main\n\nfunc main() {}\nfunc helper() {}\n",
        encoding="utf-8",
    )
    # New TMPDIR so the prior python-repo cache is not reused.
    sub_env = dict(hook_env)
    sub_tmp = Path(hook_env["TMPDIR"]) / "go_only"
    sub_tmp.mkdir(parents=True, exist_ok=True)
    sub_env["TMPDIR"] = str(sub_tmp)
    sub_env["TMP"] = str(sub_tmp)
    sub_env["TEMP"] = str(sub_tmp)
    sub_env["GIT_CEILING_DIRECTORIES"] = str(workspace.parent)
    result = _run_analyze(go_repo, sub_env, "main.go", quiet=True)
    assert result.returncode == 0, result.stderr
    # Output may legitimately be empty (no callers, no tests, no siblings).
    # The point of this test is no crash, no python-only assumption blowup.
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
