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
