"""TS/JSX probe honesty: module-unavailable must never classify as executed-ok.

RED-FIRST (2026-07-29). Baseline defect, pinned before the fix:
``_TS_PARSE_SCRIPT`` exited 0 SILENTLY when no ``typescript`` module resolved,
and ``_classify`` maps ``rc == 0`` to verdict ``ok`` (``reason=clean_exit``) —
an UNEXECUTED check reported as an executed clean parse, which feeds the
completion certificate's ``syntax_status`` head as a real PASS
(``submit_gate._SYNTAX_MAP: "ok" -> PASS``). Tier-honesty violation.

The fix makes the unavailable state POSITIVE: the probe prints the
``GT_TS_UNAVAILABLE`` sentinel and exits 3 (a code no clean parse produces),
and ``_classify`` maps the sentinel to verdict ``unavailable`` with reason
``checker_module_unavailable`` — pass-with-record downstream
(``_SYNTAX_MAP: "unavailable" -> UNKNOWN``; never a block, never an
executed-ok, never a fabricated syntax error).

MUTATION TARGETS:
  * revert the script's unavailable branch to ``process.exit(0)`` ->
    ``test_ts_probe_script_declares_unavailability`` bites.
  * drop the sentinel mapping in ``_classify`` ->
    ``test_sentinel_beats_a_clean_exit_code`` bites (rc=0 + sentinel would
    classify ``ok`` again).
"""

from __future__ import annotations

from groundtruth.runtime.edit_check import (
    _TS_PARSE_SCRIPT,
    check_edit_syntax,
)


def _fake_executor(rc, out, err):
    def _exec(cmd, cwd, timeout):
        return rc, out, err
    return _exec


def test_ts_probe_script_declares_unavailability() -> None:
    """The probe must SAY it could not run, not silently exit clean."""
    assert "if(!ts){process.exit(0)}" not in _TS_PARSE_SCRIPT
    assert "GT_TS_UNAVAILABLE" in _TS_PARSE_SCRIPT
    assert "process.exit(3)" in _TS_PARSE_SCRIPT


def test_module_unavailable_probe_is_unavailable_not_ok(tmp_path) -> None:
    """The fixed probe contract (sentinel + exit 3) classifies as
    ``unavailable`` with the named module-unavailable reason."""
    f = tmp_path / "mod.ts"
    f.write_text("const x: number = 1;\n", encoding="utf-8")
    res = check_edit_syntax(
        str(f), str(tmp_path),
        executor=_fake_executor(3, "", "GT_TS_UNAVAILABLE\n"),
    )
    assert res["verdict"] == "unavailable", res
    assert res["reason"] == "checker_module_unavailable", res
    assert res["diagnostic"] == "", res  # correct-or-quiet: no fabricated error


def test_sentinel_beats_a_clean_exit_code(tmp_path) -> None:
    """Defense in depth: even if an environment clamps the exit code to 0, the
    sentinel alone must prevent the executed-ok classification."""
    f = tmp_path / "mod.tsx"
    f.write_text("export const x = 1;\n", encoding="utf-8")
    res = check_edit_syntax(
        str(f), str(tmp_path),
        executor=_fake_executor(0, "GT_TS_UNAVAILABLE\n", ""),
    )
    assert res["verdict"] == "unavailable", res
    assert res["reason"] == "checker_module_unavailable", res


def test_clean_ts_parse_still_ok(tmp_path) -> None:
    """A genuine clean parse (module resolved, no diagnostics, exit 0, no
    sentinel) keeps the executed-ok verdict — the fix narrows nothing else."""
    f = tmp_path / "mod.ts"
    f.write_text("const x: number = 1;\n", encoding="utf-8")
    res = check_edit_syntax(
        str(f), str(tmp_path), executor=_fake_executor(0, "", ""),
    )
    assert res["verdict"] == "ok", res
    assert res["reason"] == "clean_exit", res


def test_real_ts_syntax_error_still_positive(tmp_path) -> None:
    """The probe's real error frame (exit 1 + TS diagnostic) still classifies
    as ``syntax_error`` — positive evidence is untouched."""
    f = tmp_path / "bad.ts"
    f.write_text("const x: = ;\n", encoding="utf-8")
    res = check_edit_syntax(
        str(f), str(tmp_path),
        executor=_fake_executor(
            1, "bad.ts:1:10: error TS1110: Type expected.\n", "",
        ),
    )
    assert res["verdict"] == "syntax_error", res


def test_unavailable_maps_to_unknown_in_cert_head() -> None:
    """Downstream honesty: the certificate maps ``unavailable`` to UNKNOWN
    (pass-with-record), never PASS — the executed-ok laundering is closed at
    both ends."""
    from groundtruth.runtime.submit_gate import _SYNTAX_MAP, PASS, UNKNOWN

    assert _SYNTAX_MAP["unavailable"] == UNKNOWN
    assert _SYNTAX_MAP["ok"] == PASS
