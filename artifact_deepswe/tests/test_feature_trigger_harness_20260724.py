"""TRIGGER HARNESS — for each trigger-absent DIRECT feature, INJECT its exact trigger in a
controlled input and assert the producer MANUFACTURES the fact. Proves: when the trigger is
present, the feature fires -> so the live-run absence is TRIGGER-ABSENT, not a broken feature.
Run from repo root: python -m pytest scratchpad/test_trigger_harness.py -v
"""
from __future__ import annotations
import os, sqlite3
import pytest
import gt_mini_patch as g


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))


# ── syntax_result / GT_EDIT_CHECK — trigger: an edit with a SYNTAX ERROR ──────────
def test_syntax_result_fires_on_a_syntax_error(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    bad = tmp_path / "bad.py"
    bad.write_text("def foo(:\n    return 1\n")  # deliberate syntax error
    out = g._edit_syntax_candidate("bad.py")
    assert out is not None, "REASON: producer returned None on a real syntax error (broken)"
    sev, kind, text, eb = out
    assert text and ("bad.py" in text or "syntax" in text.lower() or "error" in text.lower()), \
        f"REASON: no syntax diagnostic in delivered text: {text!r}"


# ── signature_delta / GT_PATCH_DELTA — trigger: positional param change on a called fn ──
def test_signature_delta_fires_on_param_change(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("GT_CONTRACT_MODE", "1")
    # rows: (id, name, sig, ncallers, nfiles) — foo(a, b) has callers
    rows = [(1, "foo", "def foo(a, b)", 3, 2)]
    # the edit rewrites foo with DIFFERENT positional params -> caller-breaking
    action = {"command": "str_replace", "path": "mod.py",
              "new_str": "def foo(x, y, z):\n    return x"}
    cmd = "str_replace mod.py"
    changes = g._edit_signature_changes(action, cmd, rows)
    assert "foo" in changes, f"REASON: sig-change not detected on a real param change; got {changes!r}"
    old, new = changes["foo"]
    assert old == ["a", "b"] and new == ["x", "y", "z"], f"REASON: wrong delta {old}->{new}"


# ── submit_refusal / GT_SS_SUBMIT_RED — trigger: submit with an unresolved failing test ──
def test_submit_refusal_fires_on_unresolved_red(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_ss_submit_red_fired", False, raising=False)
    # the agent's OWN last test on an edited surface was FAILING and never went green
    monkeypatch.setattr(g, "_ss_last_failing_test",
                        {"cmd": "pytest tests/test_x.py::test_foo", "rel": "mod.py"}, raising=False)
    line = g._ss_submit_red_refusal(record_candidate=False)
    assert line, "REASON: no refusal produced despite an unresolved failing test at submit (broken)"
    assert "pytest" in line or "test" in line.lower(), f"REASON: refusal missing the agent's own cmd: {line!r}"


# ── signature_delta CALLER-FALLBACK (audit 2026-07-24) — graph-coverage gap fix ──
def test_signature_delta_caller_fallback_grep(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("def foo(a, b):\n    return a\n")
    (tmp_path / "caller.py").write_text("from mod import foo\nx = foo(1, 2)\n")
    (tmp_path / "test_x.py").write_text("from mod import foo\nfoo(9, 9)\n")  # a TEST caller
    # OFF by default -> byte-identical (flag helper False)
    monkeypatch.delenv("GT_SIG_CALLER_FALLBACK", raising=False)
    assert g._sig_caller_fallback_on() is False
    # the fallback helper finds the UNINDEXED source caller, excludes the edited file + test files
    hits = g._grep_callers("foo", str(tmp_path), "mod.py")
    assert "caller.py" in hits, f"REASON: fallback did not find the unindexed caller; {hits!r}"
    assert "test_x.py" not in hits, f"REASON: LEAK — a test file caller was returned; {hits!r}"
    # excluding the file that contains the call yields no self-reference
    assert g._grep_callers("foo", str(tmp_path), "caller.py") == [] or \
        "caller.py" not in g._grep_callers("foo", str(tmp_path), "caller.py")
    # flag ON is honored
    monkeypatch.setenv("GT_SIG_CALLER_FALLBACK", "1")
    assert g._sig_caller_fallback_on() is True


# ── edit-check UNDEFINED-NAME broadening (audit 2026-07-24) — Aider-gap fix ──
def test_edit_check_names_undefined(tmp_path, monkeypatch):
    from groundtruth.runtime import edit_check as ec
    good = b"x = 1\nprint(x)\n"
    bad = b"print(undefined_thing)\n"  # VALID syntax, UNDEFINED name (a runtime NameError)
    # in-process helper: catches the undefined name, ignores a clean file (high precision)
    assert "NameError" in ec._check_py_undefined_names(bad, "m.py"), "REASON: undefined name not caught"
    assert ec._check_py_undefined_names(good, "m.py") == "", "REASON: clean file must be silent"
    (tmp_path / "m.py").write_bytes(bad)
    # OFF (default) -> byte-identical: a clean-parse file is 'ok' even with an undefined name
    monkeypatch.delenv("GT_EDIT_CHECK_NAMES", raising=False)
    assert ec.check_edit_syntax("m.py", str(tmp_path), executor=None)["verdict"] == "ok", \
        "REASON: flag OFF must stay byte-identical (verdict ok)"
    # ON -> name_error with a NameError diagnostic
    monkeypatch.setenv("GT_EDIT_CHECK_NAMES", "1")
    r_on = ec.check_edit_syntax("m.py", str(tmp_path), executor=None)
    assert r_on["verdict"] == "name_error", f"REASON: flag ON must catch undefined name; got {r_on}"
    assert "NameError" in (r_on.get("diagnostic") or ""), "REASON: missing NameError diagnostic"
    # a clean file stays 'ok' even with the flag on (no false positive)
    (tmp_path / "clean.py").write_bytes(good)
    assert ec.check_edit_syntax("clean.py", str(tmp_path), executor=None)["verdict"] == "ok"


# ── submit VERIFY-before-submit broadening (audit 2026-07-24) — AgentLens lucky-pass gap ──
def test_submit_verify_unverified_advisory(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_ss_submit_red_fired", False, raising=False)
    monkeypatch.setattr(g, "_ss_last_failing_test", None, raising=False)  # NO observed red
    monkeypatch.setattr(g, "_source_edit_count", 2, raising=False)        # edited source
    monkeypatch.setattr(g, "_oracle_test_count", 0, raising=False)        # ran no test
    # OFF (default) -> silent (byte-identical)
    monkeypatch.delenv("GT_SUBMIT_VERIFY", raising=False)
    assert g._ss_submit_red_refusal(record_candidate=False) == "", "REASON: flag OFF must be silent"
    # ON -> single-dose unverified-submit advisory
    monkeypatch.setenv("GT_SUBMIT_VERIFY", "1")
    line = g._ss_submit_red_refusal(record_candidate=False)
    assert line and "test" in line.lower(), f"REASON: no unverified-submit advisory; {line!r}"
    # a submit that DID run a test -> silent (no false block)
    monkeypatch.setattr(g, "_oracle_test_count", 1, raising=False)
    assert g._ss_submit_red_refusal(record_candidate=False) == "", "REASON: false block when tests were run"
    # no edits -> silent
    monkeypatch.setattr(g, "_oracle_test_count", 0, raising=False)
    monkeypatch.setattr(g, "_source_edit_count", 0, raising=False)
    assert g._ss_submit_red_refusal(record_candidate=False) == "", "REASON: false block with no edits"


# ── name_error verdict must render through the SCRUB (audit fix 2026-07-24) ──
def test_name_error_renders_through_scrub_not_raw_fallback():
    from groundtruth.runtime.native_render import render_syntax_error_native
    # a name_error result renders (non-empty), goes through tag-strip + _final_scrub + bound
    res = {"verdict": "name_error",
           "diagnostic": 'File "mod.py", line 3\nNameError: undefined name \'foo\''}
    block = render_syntax_error_native(res)
    assert block, "REASON: name_error must render (not '' -> no unsafe raw fallback)"
    assert "NameError" in block and "foo" in block
    # any GT tag in a (hostile) diagnostic is stripped by the renderer
    res2 = {"verdict": "name_error", "diagnostic": "NameError: <gt-leak>x</gt-leak> undefined"}
    assert "<gt-" not in render_syntax_error_native(res2), "REASON: renderer must strip gt tags"
    # a non-error verdict still renders '' (correct-or-quiet)
    assert render_syntax_error_native({"verdict": "ok", "diagnostic": "x"}) == ""
