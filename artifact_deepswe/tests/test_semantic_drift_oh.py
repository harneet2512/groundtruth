"""Pins for the semantic-drift channel on BOTH harness topologies.

mini/DeepSWE (_augment_output) reads LIVE source via _sem_seed/_semantic_drift_candidate —
it works because that harness runs where the source tree is readable.

OpenHands runs HOST-side (only graph.db is copy_from'd out of the container; the source tree
is NOT), so a live-source read is INERT there. The OH wire therefore reads the DIFF (which OH
holds host-side) via oh_gt_full_wrapper._semantic_drift_from_diff, reusing mini's _sem_extract
on removed-vs-added hunk lines. THE BUGS this file guards:
  (1) a live-source-read wire that never fires in OH's topology (fixed -> diff-based);
  (2) a test-file diff echoing its guard string into <gt-nudge> (gated via _is_test_path,
      incl .test.ts / conftest.py / CamelCase FooTest.java the old regex missed). Leak=0.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

import gt_mini_patch as g


# ---- mini/DeepSWE path: live-source producer (host FS == source tree) --------
def _guarded_src() -> str:
    return "def h(x):\n    if x is None:\n        return 0\n    return x\n"


def test_mini_seed_then_drop_fires(tmp_path, monkeypatch):
    f = tmp_path / "mod.py"
    f.write_text(_guarded_src())
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._sem_cache.pop("mod.py", None)
    g._sem_seed("mod.py")
    assert "mod.py" in g._sem_cache
    f.write_text("def h(x):\n    return x\n")
    cand = g._semantic_drift_candidate("mod.py")
    assert cand is not None and 'reason="semantic_drift"' in cand[1]


def test_mini_no_seed_is_silent(tmp_path, monkeypatch):
    f = tmp_path / "mod.py"
    f.write_text("def h(x):\n    return x\n")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._sem_cache.pop("mod.py", None)
    assert g._semantic_drift_candidate("mod.py") is None


# ---- OH path: diff-based helper (host-side; the live-source read is inert) ----
_WRAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))
sys.path.insert(0, _WRAP_DIR)
for _mod in ("litellm", "cost_tracking"):
    sys.modules.setdefault(_mod, SimpleNamespace(
        model_cost={}, success_callback=[], completion=lambda *a, **k: None,
        acompletion=None, completion_cost=lambda *a, **k: 0.0,
        track_cost=lambda *a, **k: None, CostTracker=object))
try:
    import oh_gt_full_wrapper as _ohgt
    _drift = _ohgt._semantic_drift_from_diff
except Exception:  # heavy sibling deps unavailable -> skip only the OH-path pins
    _drift = None

oh = pytest.mark.skipif(_drift is None, reason="oh_gt_full_wrapper import unavailable")

_DEL_DIFF = ("--- a/f.py\n+++ b/f.py\n@@ -1,4 +1,2 @@\n def h(x):\n"
             "-    if x is None:\n-        return 0\n     return x")


@oh
def test_oh_diff_delete_guard_fires():
    out = _drift(_DEL_DIFF, "src/f.py", g._sem_extract)
    assert out and 'reason="semantic_drift"' in out
    assert "confirm that deletion is intended" in out


@oh
def test_oh_diff_pure_addition_is_silent():
    add = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,3 @@\n def h(x):\n+    log(x)\n     return x"
    assert _drift(add, "src/f.py", g._sem_extract) == ""


@oh
@pytest.mark.parametrize("rel", ["tests/test_f.py", "src/f.test.ts", "conftest.py", "a/FooTest.java"])
def test_oh_diff_test_paths_gated(rel):
    """The deletion is real, but a test-file edit must NEVER echo its guard string (leak=0).
    Covers the dot-form/.conftest/CamelCase paths the pre-2026-07-07 TEST_PATH_RE missed."""
    assert _drift(_DEL_DIFF, rel, g._sem_extract) == ""


@oh
def test_oh_diff_empty_is_silent():
    assert _drift("", "src/f.py", g._sem_extract) == ""
