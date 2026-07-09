"""GT_OBLIGATIONS_V2 Move-2 — the ground-truth submit guard (_l5_unresolved_build_guard).

Replaces the retired T2 completion checklist. The verify axis deliberately
suppresses the compile/type-check class as "actionable feedback" (correct while
EDITING); this guard closes the SUBMIT-boundary hole where true-myth's agent read
7 tsc errors, called them "strict-mode noise", and shipped over them with ZERO
GT pushback. These pin: it fires ONLY on an unresolved compile/type failure at
VERIFY/SUBMIT after a source edit, on a REAL build/test command; it is silent
mid-EDIT, on env failures, on a clean run, on a scratch command, and with the
flag off; it fires ONCE; and it NEVER leaks a source/type/test identifier."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TS_FAIL = (
    "src/task.ts(42,3): error TS2322: Type 'Result<T>' is not assignable to 'T'.\n"
    "src/toolbelt.ts(9,1): error TS2551: Property 'firstJust' does not exist.\n"
    "Found 7 errors.\n"
)
_CLEAN = "Test Files  16 passed (16)\n     Tests  1128 passed (1128)\n  no type errors\n"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_OBLIGATIONS_V2", "1")
    repo = str(Path(__file__).resolve().parents[1])
    for p in (repo, str(Path(repo) / "artifact_deepswe")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import gt_mini_patch as mod  # noqa: PLC0415
    # clean, activated state: flag on (artifact present), post-edit, GT-on
    mod._obligations_v2_cache = None
    mod._l5_build_fail_fired = False
    mod._GT_BASELINE = False
    mod._source_edit_count = 1
    (tmp_path / "gt_obligations_v2.json").write_text(
        json.dumps({"obligations_version": 2, "clauses": []}), encoding="utf-8"
    )
    return mod


def _call(mod, cmd, out, phase=None):
    return mod._l5_unresolved_build_guard(cmd, out, phase or mod.Phase.VERIFY)


# ── fires on the real hole (red→green) ───────────────────────────────────────
def test_fires_on_typecheck_failure_at_verify(m):
    got = _call(m, "npx vitest run", _TS_FAIL)
    assert got and "unresolved_build_failure" in got
    assert "2 unresolved" in got  # two TS#### markers -> count 2 (not "Found 7")


def test_fires_on_direct_build_command(m):
    got = _call(m, "tsc --noEmit", "index.ts(1,1): error TS2551: nope.\n")
    assert got and "1 unresolved" in got


# ── mutation bite: without the failure marker it MUST be silent ──────────────
def test_silent_on_clean_run(m):
    assert _call(m, "npx vitest run", _CLEAN) == ""


# ── phase gate: mid-EDIT compile errors are normal feedback, never a steer ───
def test_silent_at_edit_phase(m):
    assert _call(m, "tsc --noEmit", _TS_FAIL, phase=m.Phase.EDIT) == ""


def test_fires_at_submit_phase(m):
    assert _call(m, "tsc --noEmit", _TS_FAIL, phase=m.Phase.SUBMIT) != ""


# ── correct-or-quiet suppressions ────────────────────────────────────────────
def test_silent_on_env_failure(m):
    # an env/tooling failure says nothing about the agent's build
    out = "ModuleNotFoundError: No module named 'x'\nindex.ts(1,1): error TS2322: bad\n"
    assert _call(m, "tsc --noEmit", out) == ""


def test_silent_without_source_edit(m):
    # a pristine-checkout compile error is the repo's own state
    m._source_edit_count = 0
    assert _call(m, "tsc --noEmit", _TS_FAIL) == ""


def test_silent_on_scratch_command(m):
    # a file view is not a build invocation — cannot gate a submit
    assert _call(m, "cat src/task.ts", _TS_FAIL) == ""


# ── dose: fires once per run ─────────────────────────────────────────────────
def test_fires_once(m):
    assert _call(m, "tsc --noEmit", _TS_FAIL) != ""
    assert _call(m, "tsc --noEmit", _TS_FAIL) == ""  # latched


# ── baseline arm never steers ────────────────────────────────────────────────
def test_baseline_silent(m):
    m._GT_BASELINE = True
    assert _call(m, "tsc --noEmit", _TS_FAIL) == ""


# ── flag off (no artifact) = byte-identical silence ──────────────────────────
def test_flag_off_silent(m, tmp_path):
    (tmp_path / "gt_obligations_v2.json").unlink()
    m._obligations_v2_cache = None
    assert _call(m, "tsc --noEmit", _TS_FAIL) == ""


# ── leak invariant: never quote a source/type/test identifier ────────────────
def test_no_identifier_leak(m):
    got = _call(m, "npx vitest run", _TS_FAIL)
    assert got
    for tok in ("TS2322", "TS2551", "task.ts", "toolbelt", "firstJust", "Result"):
        assert tok not in got
    assert not m._V2_LEAK_TEST_RE.search(got)
