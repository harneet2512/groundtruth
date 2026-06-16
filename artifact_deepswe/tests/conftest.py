"""Test isolation for the DeepSWE-twin suite.

The handoff fail-closed guard (`_guard_handoff_db` -> GTHandoffEmptyError) keys on
substrate-mode env (GT_HOST_GRAPH_DB / GT_CERT_DIR / GT_PORTABLE_SUBSTRATE / GT_PROOF_MODE)
and a MODULE-GLOBAL cache (`_handoff_guard_state`). A test that exercises substrate mode
(test_a2_handoff_failclosed) leaves that env set + the cache populated; a LATER test that
runs a producer (test_verify_structural_risk_wire) then sees `_substrate_active()` True and
fail-closes on its stub `/nonexistent.db` — a pass-in-isolation / fail-in-suite ordering trap.

In a real benchmark run pier spawns a FRESH PROCESS per task, so this leak cannot occur in
production — the guard (fail-closed on an empty handoff) is correct and is NOT changed here.
This fixture only makes the in-process test suite hermetic: reset the per-run module state +
substrate env before each test, restore the outer env after.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SUBSTRATE_ENV = ("GT_HOST_GRAPH_DB", "GT_CERT_DIR", "GT_PORTABLE_SUBSTRATE", "GT_PROOF_MODE")


@pytest.fixture(autouse=True)
def _hermetic_gt_state():
    saved = {k: os.environ.get(k) for k in _SUBSTRATE_ENV}
    for k in _SUBSTRATE_ENV:
        os.environ.pop(k, None)
    try:
        import gt_mini_patch as g
        g._handoff_guard_state.clear()
    except Exception:  # noqa: BLE001 — best-effort isolation, never break collection
        pass
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
