"""Stage-1: G-2 submit gate — the STALENESS + ATTRIBUTION fixes (2026-07-09).

Two false-block bugs on the enforcement chokepoint, both fixed:
- STALENESS: `_last_covering_result` is cached ONCE per task; a RED cached at the
  first risky edit would block a submit MANY turns later even after the agent FIXED
  the file. Fix: a cached FAIL is re-run fresh at submit; a cached non-fail is a
  safe fast-path.
- ATTRIBUTION: the gate blocked on ANY covering fail, including a pre-existing red
  the edit never caused. Fix: attribution-gate the submit-time fail (parity with the
  post_edit path). Un-attributed -> non-blocking (invariant ②, FP=0).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ARTIFACT = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if _ARTIFACT not in sys.path:
    sys.path.insert(0, _ARTIFACT)

gmp = pytest.importorskip("gt_mini_patch")
import groundtruth.runtime.covering_runner as cr  # noqa: E402


class Submitted(Exception):
    """Name-based stand-in for minisweagent's completion signal (_is_submitted_exc)."""


# --------------------------------------------------------------------------- #
# _gt_submit_covering — staleness
# --------------------------------------------------------------------------- #
def test_cached_green_is_fastpath_no_rerun(monkeypatch, tmp_path):
    monkeypatch.setattr(gmp, "_last_covering_result", {"verdict": "pass"}, raising=False)
    monkeypatch.setattr(gmp, "_covering_tests_for_symbols", lambda toks: [])
    calls = {"n": 0}
    def _tracked(*a, **k):
        calls["n"] += 1
        return {"verdict": "fail"}
    monkeypatch.setattr(cr, "run_covering_tests", _tracked)
    res, _files = gmp._gt_submit_covering(str(tmp_path))
    assert res == {"verdict": "pass"}
    assert calls["n"] == 0                       # trusted the cached non-fail, no re-run


def test_cached_fail_is_stale_reruns_fresh(monkeypatch, tmp_path):
    # MUTATION: flip `!= "fail"` -> `== "fail"` in the fast-path guard: the stale fail
    # is returned instead of the fresh pass -> this bites.
    (tmp_path / "t_mod.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(gmp, "_last_covering_result", {"verdict": "fail"}, raising=False)
    monkeypatch.setattr(gmp, "_covering_tests_for_symbols",
                        lambda toks: [{"file": "t_mod.py"}])
    monkeypatch.setattr(cr, "run_covering_tests",
                        lambda *a, **k: {"verdict": "pass", "ran": ["t_mod.py"]})
    res, files = gmp._gt_submit_covering(str(tmp_path))
    assert res == {"verdict": "pass", "ran": ["t_mod.py"]}   # FRESH, not the stale fail
    assert files == ["t_mod.py"]


# --------------------------------------------------------------------------- #
# _gt_gate_submit_exception — attribution
# --------------------------------------------------------------------------- #
@pytest.fixture
def _gate_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_gt_submit_hygiene", lambda root: None)
    monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/foo.py"}, raising=False)
    monkeypatch.setattr(gmp, "_oracle_delivered_hashes", set(), raising=False)
    monkeypatch.setattr(gmp, "_gt_submit_bounce_count", 0, raising=False)
    return tmp_path


def test_unattributed_fail_does_not_block(_gate_env, monkeypatch):
    # a fresh covering FAIL that the edit did NOT cause must NOT block the submit.
    # MUTATION: delete the attribution block -> this returns a block dict -> bites.
    monkeypatch.setattr(gmp, "_gt_submit_covering",
                        lambda root: ({"verdict": "fail", "ran": ["t.py"]}, ["t.py"]))
    monkeypatch.setattr(cr, "is_red_attributable", lambda *a, **k: False)
    out = gmp._gt_gate_submit_exception(object(), {"command": "submit"}, Submitted())
    assert out is None                           # ALLOW (submission proceeds)


def test_attributed_fail_blocks_native(_gate_env, monkeypatch):
    monkeypatch.setattr(gmp, "_gt_submit_covering",
                        lambda root: ({"verdict": "fail", "ran": ["t.py"]}, ["t.py"]))
    monkeypatch.setattr(cr, "is_red_attributable", lambda *a, **k: True)
    out = gmp._gt_gate_submit_exception(object(), {"command": "submit"}, Submitted())
    assert isinstance(out, dict)
    assert out.get("returncode") == 1
    assert "<gt-" not in (out.get("output") or "").lower()   # native, no GT tag


def test_non_submit_exception_propagates(_gate_env, monkeypatch):
    # a real error (not a Submitted) must propagate unchanged -> gate returns None.
    monkeypatch.setattr(gmp, "_gt_submit_covering", lambda root: (None, []))
    out = gmp._gt_gate_submit_exception(object(), {"command": "x"}, ValueError("boom"))
    assert out is None
