"""B1 finalization on the mini-swe-agent seam — Stage-1.

Pins the `_executed_covering_emission` helper wired into gt_mini_patch's
verify-horizon: byte-identical when GT_VERIFY_EXECUTE is off, and on a real
attributed covering CRASH it returns the Format-D anonymized native failure
block (signal kept, zero test identity). Also pins the honest SEAM scope limit:
the seam calls native_render.is_edit_attributed (frames only), so an assertion
failure (no agent-source frame) is NOT attributed at the SEAM -> stays quiet.

The green->base->red DIFFERENTIAL that rescues assertion failures is now BUILT at
the engine level (covering_runner.is_red_attributable / differential_attribution,
tested in test_b1_differential_attribution.py); wiring gt_mini_patch's seam to
call it (passing repo_root + covering_files) is owed (Wave-2). Until then this
frames-only seam behavior is the correct, honest thing to pin.
"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

_ARTIFACT = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if _ARTIFACT not in sys.path:
    sys.path.insert(0, _ARTIFACT)

gmp = pytest.importorskip("gt_mini_patch")
from groundtruth.runtime.native_render import contains_gt_tag, contains_test_identity  # noqa: E402


def _graph(path: Path, foo_file: str, test_file: str) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
                "file_path TEXT, is_test INT, language TEXT)")
    con.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
                "type TEXT, resolution_method TEXT, confidence REAL)")
    con.execute("INSERT INTO nodes VALUES (1,'Function','foo',?,0,'python')", (foo_file,))
    con.execute("INSERT INTO nodes VALUES (2,'Function','test_foo',?,1,'python')", (test_file,))
    con.execute("INSERT INTO edges VALUES (1,2,1,'CALLS','import',0.95)")
    con.commit(); con.close()


def test_off_path_is_byte_identical(monkeypatch):
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    cov = [{"file": "test_mod.py", "confidence": 0.9}]
    assert gmp._executed_covering_emission(cov, {"mod.py"}, {"foo"}) is None
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", True, raising=False)
    assert gmp._executed_covering_emission(cov, {"mod.py"}, {"foo"}) is None


@pytest.mark.skipif(__import__("shutil").which("pytest") is None, reason="pytest not on PATH")
def test_on_path_delivers_format_d_for_attributed_crash(tmp_path, monkeypatch):
    repo = tmp_path
    # foo() CRASHES (TypeError raised inside the agent's own source) -> the
    # traceback carries a mod.py frame -> attributed by frame.
    (repo / "mod.py").write_text("def foo():\n    return 1 + 'x'\n")
    (repo / "test_mod.py").write_text(textwrap.dedent("""
        from mod import foo
        def test_foo():
            foo()
    """))
    db = repo / "graph.db"
    _graph(db, "mod.py", "test_mod.py")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(gmp, "_root", lambda: str(repo))
    monkeypatch.setattr(gmp, "_db_path", lambda: str(db))
    monkeypatch.setattr(gmp, "_action_count", 5, raising=False)
    monkeypatch.setattr(gmp, "_last_test_step", None, raising=False)
    cov = gmp._covering_tests_for_symbols({"foo"})
    assert cov and cov[0]["file"] == "test_mod.py"
    block = gmp._executed_covering_emission(cov, {"mod.py"}, {"foo"})
    assert block, "attributed crash should deliver a Format-D block"
    assert "TypeError" in block                       # signal kept
    assert "test_foo" not in block and "test_mod" not in block and "::" not in block
    assert not contains_gt_tag(block)
    assert not contains_test_identity(block, ["test_mod.py"])
    # executed verdict drove the verify axis (V-3)
    assert gmp._last_test_outcome_failed is True


@pytest.mark.skipif(__import__("shutil").which("pytest") is None, reason="pytest not on PATH")
def test_on_path_quiet_on_unattributed_assertion(tmp_path, monkeypatch):
    repo = tmp_path
    # foo() returns a wrong VALUE (no crash) -> the assert lives in the test, no
    # agent frame in the traceback -> NOT attributed BY THE SEAM (frames-only) ->
    # stays quiet. The engine-level differential (is_red_attributable) WOULD deliver
    # here; the seam does not yet call it (Wave-2 wiring owed) -> block is None.
    (repo / "mod.py").write_text("def foo():\n    return 1\n")
    (repo / "test_mod.py").write_text(textwrap.dedent("""
        from mod import foo
        def test_foo():
            assert foo() == 2
    """))
    db = repo / "graph.db"
    _graph(db, "mod.py", "test_mod.py")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(gmp, "_root", lambda: str(repo))
    monkeypatch.setattr(gmp, "_db_path", lambda: str(db))
    monkeypatch.setattr(gmp, "_action_count", 5, raising=False)
    monkeypatch.setattr(gmp, "_last_test_step", None, raising=False)
    cov = gmp._covering_tests_for_symbols({"foo"})
    block = gmp._executed_covering_emission(cov, {"mod.py"}, {"foo"})
    assert block is None  # correct-or-quiet: no false red on an unattributed fail


# --------------------------------------------------------------------------- #
# TASK 2 — the green->base->red DIFFERENTIAL wired into the seam. Same shape as
# the assertion test above, but the base is a GIT commit that was GREEN and the
# working-tree edit is RED. The differential (is_red_attributable) now attributes
# the value failure the frames-only seam could not -> DELIVERS a Format-D block.
# RED before the wiring (frames-only is_edit_attributed): the mutation that reverts
# to frames-only makes this test bite.
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(__import__("shutil").which("pytest") is None, reason="pytest not on PATH")
@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="git not on PATH")
def test_on_path_delivers_format_d_for_git_base_assertion(tmp_path, monkeypatch):
    repo = tmp_path
    # BASE (committed at HEAD) is GREEN: foo() returns the asserted value.
    (repo / "mod.py").write_text("def foo():\n    return 2\n")
    (repo / "test_mod.py").write_text(textwrap.dedent("""
        from mod import foo
        def test_foo():
            assert foo() == 2
    """))
    _git(repo, "init")
    # Explicit user config so the commit works on Windows CI (no global git identity).
    _git(repo, "config", "user.email", "gt@example.com")
    _git(repo, "config", "user.name", "gt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "green base")
    # WORKING-TREE edit -> RED: foo() now returns a wrong value (no crash, pure value
    # failure -> the assert lives in the test, NO agent-source frame). Frames-only would
    # stay quiet; the differential (base GREEN, current RED) attributes it.
    (repo / "mod.py").write_text("def foo():\n    return 1\n")
    db = repo / "graph.db"
    _graph(db, "mod.py", "test_mod.py")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(gmp, "_root", lambda: str(repo))
    monkeypatch.setattr(gmp, "_db_path", lambda: str(db))
    monkeypatch.setattr(gmp, "_action_count", 5, raising=False)
    monkeypatch.setattr(gmp, "_last_test_step", None, raising=False)
    cov = gmp._covering_tests_for_symbols({"foo"})
    assert cov and cov[0]["file"] == "test_mod.py"
    block = gmp._executed_covering_emission(cov, {"mod.py"}, {"foo"})
    assert block, "git-base attributed assertion RED must deliver a Format-D block"
    # leak invariant: zero test identity, zero GT tag.
    assert "test_foo" not in block and "test_mod" not in block and "::" not in block
    assert not contains_gt_tag(block)
    assert not contains_test_identity(block, ["test_mod.py"])
    assert gmp._last_test_outcome_failed is True


# --------------------------------------------------------------------------- #
# TASK 1 — the SUBMIT GATE (G-2). Driven fake-loop tests over
# `_gt_gate_submit_exception` (the true chokepoint: env.execute raises `Submitted`
# for a submit, and _wrap_execute catches it here). A fake exception class named
# `Submitted` satisfies `_is_submitted_exc` without importing minisweagent (whose
# banner crashes on Windows cp1252).
# --------------------------------------------------------------------------- #
class _Submitted(Exception):
    pass


_Submitted.__name__ = "Submitted"  # `_is_submitted_exc` keys on the class name


def _submit_env(monkeypatch, hygiene, covering):
    """Common submit-gate fixture: flag on, fresh module state, injected hygiene +
    covering. `hygiene`/`covering` may be a dict/None or a zero-arg callable."""
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(gmp, "_root", lambda: "/repo", raising=False)
    monkeypatch.setattr(gmp, "_oracle_delivered_hashes", set(), raising=False)
    monkeypatch.setattr(gmp, "_gt_submit_bounce_count", 0, raising=False)
    monkeypatch.setattr(gmp, "_GT_SUBMIT_MAX_BOUNCES", 1, raising=False)
    hyg = hygiene if callable(hygiene) else (lambda root: hygiene)

    # _gt_submit_covering now returns (result, files). Provide covering FILES whenever a
    # FAIL is injected so the attribution gate has something to attribute; a green/None
    # verdict carries no files (attribution never runs on it).
    def _cov(root):
        c = covering(root) if callable(covering) else covering
        files = ["t.py"] if (c or {}).get("verdict") == "fail" else []
        return c, files

    monkeypatch.setattr(gmp, "_gt_submit_hygiene", hyg, raising=False)
    monkeypatch.setattr(gmp, "_gt_submit_covering", _cov, raising=False)
    # A covering RED reaching the gate in these fixtures IS the agent's own edit, so
    # mark it attributable — these tests pin the bounce/dedup/fail-open MECHANICS; the
    # attribution decision itself is pinned in test_g2_submit_gate_staleness.py.
    import groundtruth.runtime.covering_runner as _cr
    monkeypatch.setattr(_cr, "is_red_attributable", lambda *a, **k: True, raising=False)


def _assert_native_refusal(rej: dict) -> str:
    assert isinstance(rej, dict) and rej.get("returncode") == 1
    txt = rej.get("output") or ""
    assert "pre-commit hook failed" in txt and "commit aborted" in txt
    assert "<gt-" not in txt.lower()          # zero GT tags
    assert "::" not in txt                     # zero nodeid
    assert not contains_gt_tag(txt)
    assert not contains_test_identity(txt)
    return txt


_BINARY_HYGIENE = {"blocking": True, "reason": "binary_file_in_diff",
                   "detail": "refusing to commit binary artifact(s): weights.bin"}
_COVERING_RED = {"verdict": "fail", "reason": "ran",
                 "stdout_tail": "", "stderr_tail": "", "failing_test_names": []}


def test_submit_gate_flag_off_is_noop(monkeypatch):
    """Flag off -> no gate code runs -> None (submission proceeds). Byte-identical."""
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    assert gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted()) is None


def test_submit_gate_non_submit_exception_propagates(monkeypatch):
    """A real (non-Submitted) error is NOT a submit -> None -> caller re-raises it."""
    _submit_env(monkeypatch, _BINARY_HYGIENE, None)
    assert gmp._gt_gate_submit_exception(object(), {"command": "x"}, ValueError("boom")) is None


def test_submit_gate_clean_submit_passes_untouched(monkeypatch):
    """Clean hygiene + a GREEN covering verdict -> allow -> None (submission proceeds)."""
    _submit_env(monkeypatch, {"blocking": False, "reason": ""}, {"verdict": "pass"})
    assert gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted()) is None
    assert gmp._gt_submit_bounce_count == 0


def test_submit_gate_covering_red_bounces(monkeypatch):
    """A covering-RED submit BOUNCES once with the native refusal (covering axis)."""
    _submit_env(monkeypatch, {"blocking": False, "reason": ""}, _COVERING_RED)
    rej = gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted())
    txt = _assert_native_refusal(rej)
    assert "covering" in txt.lower()
    assert gmp._gt_submit_bounce_count == 1


def test_submit_gate_bounces_once_then_fails_open(monkeypatch):
    """Dirty submit -> BLOCK once; a SECOND (differently-dirty) submit FAILS OPEN
    (gate_overridden) so the gate never deadlocks a run at reward 0.

    The two submits carry DIFFERENT hygiene detail (different refusal hash) so the
    dedup ledger cannot mask the bounce-cap behavior — the cap is the sole guard.
    MUTATION `disable the bounce cap` (raise _GT_SUBMIT_MAX_BOUNCES) makes the second
    submit BLOCK instead of allow -> this test bites."""
    calls = {"n": 0}

    def _hyg(root):
        calls["n"] += 1
        return {"blocking": True, "reason": "binary_file_in_diff",
                "detail": f"refusing to commit binary artifact(s): blob{calls['n']}.bin"}

    _submit_env(monkeypatch, _hyg, None)
    # FIRST submit -> BLOCK, bounce -> 1.
    r1 = gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted())
    _assert_native_refusal(r1)
    assert gmp._gt_submit_bounce_count == 1
    # SECOND submit (still dirty, DIFFERENT detail) -> FAIL-OPEN -> allow (None).
    r2 = gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted())
    assert r2 is None, "second submit must fail OPEN (gate_overridden), not block again"


def test_submit_gate_dedup_suppresses_identical_refusal(monkeypatch):
    """The refusal stamps its content hash into _oracle_delivered_hashes, and an
    IDENTICAL refusal never re-emits. Isolated from the bounce cap by resetting the
    bounce counter to 0 before the second (identical) attempt. MUTATION `break the
    dedup stamp` (drop the _oracle_delivered_hashes.add) makes the stamp assertion
    bite AND the second identical attempt re-block."""
    _submit_env(monkeypatch, _BINARY_HYGIENE, None)  # SAME hygiene each call
    r1 = gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted())
    txt = _assert_native_refusal(r1)
    hc = "c:" + __import__("hashlib").sha256(txt.encode("utf-8")).hexdigest()[:8]
    assert hc in gmp._oracle_delivered_hashes, "refusal must stamp the dedup ledger"
    # reset the cap guard so DEDUP alone decides the second identical attempt.
    gmp._gt_submit_bounce_count = 0
    r2 = gmp._gt_gate_submit_exception(object(), {"command": "x"}, _Submitted())
    assert r2 is None, "an identical refusal must be deduped (never re-emit)"
