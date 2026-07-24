"""AUDIT 2026-07-24 — LANGUAGE COVERAGE: .ts/.tsx/.jsx had NO at-edit checker, so 13 of 17 edit
opportunities on the superjson (TypeScript) task returned dependency_unavailable:unsupported_language
in run 30121930273 — 76% of edits unverifiable. The substrate already bundles `typescript`."""
from __future__ import annotations
from groundtruth.runtime import edit_check as ec


def test_ts_tsx_jsx_now_have_a_checker():
    for ext in (".ts", ".tsx", ".jsx"):
        cmd = ec._build_check_command(ext, f"m{ext}")
        assert cmd is not None, f"REGRESSION: {ext} has no at-edit checker (the 13/17 gap)"
        assert cmd[0] == "node" and cmd[1] == "-e", f"{ext}: unexpected probe {cmd[:2]}"
        assert f"m{ext}" in cmd, f"{ext}: target path not passed to the probe"


def test_ts_probe_is_parse_only_and_quiet_without_the_module():
    """Honesty contract: PARSE diagnostics only (no type/module errors), and a missing
    `typescript` module exits 0 rather than fabricating a syntax error."""
    s = ec._TS_PARSE_SCRIPT
    assert "parseDiagnostics" in s, "probe must use PARSE diagnostics only (no type checking)"
    assert "process.exit(0)" in s, "probe must exit 0 (quiet) when typescript is unavailable"
    assert "createSourceFile" in s
    # never invokes a type checker / program construction
    assert "createProgram" not in s and "getSemanticDiagnostics" not in s


def test_unsupported_languages_still_return_none():
    """Correct-or-quiet preserved: languages we cannot soundly check stay unavailable."""
    for ext in (".rs", ".java", ".zig", ""):
        assert ec._build_check_command(ext, "m" + ext) is None


# ---------------------------------------------------------------------------
# EXECUTION PROOF (added 2026-07-24 after audit).
# The tests above assert PROPERTIES OF THE SCRIPT SOURCE — they would pass with a
# completely dead checker, which is precisely the trap that let a dead-by-
# construction name-check ship. `.ts` never takes the in-process path, so the only
# honest proof is running the real toolchain end to end.
#
# Module resolution for `node -e` starts at CWD, so these run with repo_root at a
# directory where `typescript` resolves. In-container there are two chances: the
# repo's own node_modules, then /opt/gt/node/lib/node_modules/typescript, which
# Dockerfile.gt-substrate:74 installs via `npm install -g --prefix /opt/gt/node`.
# ---------------------------------------------------------------------------
import os
import shutil
import subprocess

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _typescript_resolvable() -> bool:
    if not shutil.which("node"):
        return False
    try:
        return subprocess.run(["node", "-e", "require('typescript')"], cwd=_REPO,
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


_needs_ts = pytest.mark.skipif(
    not _typescript_resolvable(),
    reason="typescript not resolvable here; in-container it comes from the repo or /opt/gt/node",
)


@pytest.fixture
def ts_file(tmp_path_factory):
    """Write inside the repo so `node -e` can resolve typescript from CWD."""
    d = os.path.join(_REPO, ".tmp_ts_edit_check_test")
    os.makedirs(d, exist_ok=True)

    def _make(name, body):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return os.path.relpath(p, _REPO).replace("\\", "/")

    yield _make
    shutil.rmtree(d, ignore_errors=True)


@_needs_ts
def test_real_ts_syntax_error_is_caught(ts_file):
    """The 76%-coverage-hole fix: a genuine parse error must be REPORTED."""
    rel = ts_file("broken.ts", "function f(a: number {\n  return a;\n}\n")
    res = ec.check_edit_syntax(rel, _REPO)
    assert res["verdict"] == "syntax_error", \
        f"TS checker is DEAD — a missing ')' returned {res['verdict']!r} ({res['reason']!r})"
    assert "TS" in res["diagnostic"], f"expected the compiler's own wording: {res['diagnostic']!r}"


@_needs_ts
def test_real_clean_ts_is_ok(ts_file):
    rel = ts_file("clean.ts", "export const f = (a: number): number => a + 1;\n")
    assert ec.check_edit_syntax(rel, _REPO)["verdict"] == "ok"


@_needs_ts
def test_type_error_is_NOT_reported_as_syntax(ts_file):
    """PARSE-ONLY by design: a type error is not a syntax error. Correct-or-quiet —
    reporting it would be a fabricated verdict from a checker that never type-checks."""
    rel = ts_file("typeerr.ts", "const x: number = 'not a number';\n")
    assert ec.check_edit_syntax(rel, _REPO)["verdict"] == "ok"


@_needs_ts
def test_tsx_is_really_parsed(ts_file):
    rel = ts_file("broken.tsx", "export const C = () => <div>hi</div;\n")
    assert ec.check_edit_syntax(rel, _REPO)["verdict"] == "syntax_error"


def test_missing_typescript_degrades_to_quiet_not_to_a_fake_error(tmp_path):
    """An empty CWD cannot resolve typescript; the probe must exit 0 => never a
    fabricated syntax_error. This is the correct-or-quiet floor."""
    p = tmp_path / "broken.ts"
    p.write_text("function f(a: number {\n")
    assert ec.check_edit_syntax(str(p), str(tmp_path))["verdict"] in ("ok", "unavailable")
