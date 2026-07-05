"""Tier-2: multi-language closure rebuilt ONCE — the _closure_action decision (red->green).

The transitive-closure sidecar is a WHOLE-GRAPH property recomputed over the FINAL edge set. The
per-language LSP loop used to rebuild it once per language (only the last survives = K-1 wasted
rebuilds). Tier-2 defers each per-language rebuild (GT_DEFER_CLOSURE_REBUILD) to ONE orchestrator-
owned rebuild after the loop. `_closure_action` is the single, pure decision both sides share.

Asserted:
  1. BASELINE byte-identical: defer=False reproduces the historical per-pass branch EXACTLY
     (rebuild iff edges changed, else stamp) — the single-language / standalone guarantee.
  2. MULTI-LANG defers: defer=True -> "defer" regardless of `changed` (the pass skips; caller owns).
  3. ORCHESTRATOR contract: the post-loop call `_closure_action(False, Σeffective)` is never "defer"
     — it is "rebuild" iff any language mutated edges, else "stamp" (mirrors the per-pass rule once).
  4. DETERMINISTIC + pure: same inputs -> same output.

RED proof (mutation): drop the `if defer:` branch in _closure_action and tests 2+3-defer reddens.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from groundtruth.resolve import _closure_action  # noqa: E402


# ── 1. BASELINE byte-identical: defer=False == the historical rebuild-iff-changed-else-stamp ─────
@pytest.mark.parametrize("changed,expected", [
    (0, "stamp"), (1, "rebuild"), (12, "rebuild"), (39, "rebuild"), (5000, "rebuild"),
])
def test_baseline_defer_off_is_historical_behavior(changed, expected):
    assert _closure_action(False, changed) == expected


# ── 2. MULTI-LANG pass defers regardless of change (the RED target) ──────────────────────────────
@pytest.mark.parametrize("changed", [0, 1, 12, 39, 5000])
def test_multilang_pass_defers(changed):
    assert _closure_action(True, changed) == "defer", \
        "a deferred per-language pass must NOT rebuild (orchestrator owns the one rebuild) — RED if not"


# ── 3. ORCHESTRATOR contract: the post-loop call never defers; rebuild iff any edge changed ──────
@pytest.mark.parametrize("total_effective,expected", [
    (0, "stamp"),      # katex python-only-no-op shape: no edges changed -> stamp, no rebuild
    (1, "rebuild"),    # katex js: deleted 1
    (39, "rebuild"),   # katex ts: verified 26 + corrected 12 + deleted 1
])
def test_orchestrator_post_loop_decision(total_effective, expected):
    # the orchestrator always passes defer=False for its single post-loop rebuild
    assert _closure_action(False, total_effective) == expected
    assert _closure_action(False, total_effective) != "defer"


# ── 4. DETERMINISTIC + pure ──────────────────────────────────────────────────────────────────────
def test_deterministic():
    for d in (True, False):
        for c in (0, 1, 7, 100):
            first = _closure_action(d, c)
            assert all(_closure_action(d, c) == first for _ in range(50))


def test_only_three_outcomes():
    seen = {_closure_action(d, c) for d in (True, False) for c in range(0, 6)}
    assert seen <= {"defer", "rebuild", "stamp"}
