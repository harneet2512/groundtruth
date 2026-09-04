"""FIX 6 (2026-06-11 measurement-gate closeout) — V is never inert for
never-test agents.

The V estimator (verification-cycle cost) was the median of observed
EDIT->TEST spans; an agent who NEVER tests (the escalation ladder's TARGET
population — 2/9 of the frozen oracle corpus) produced an empty span list and
V silently fell back to the static 25. Proxy: for the never-test agent, V is
estimated from its EDIT->EDIT pace (median span between consecutive source
edits — the agent's own working cadence determines how much budget a
verification cycle would cost it). Still dynamic (from the agent's own data),
still railed [V_MIN, V_MAX]; observed EDIT->TEST spans always win when they
exist; a single edit (no pace yet) keeps the default.

Red->green: an agent who edits but never tests -> V from edit-to-edit pace,
not the static 25.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load_patch(name: str):
    prev = {k: os.environ.get(k) for k in ("GT_BASELINE", "GT_STEP_LIMIT")}
    os.environ["GT_BASELINE"] = "1"
    os.environ["GT_STEP_LIMIT"] = "300"
    try:
        spec = importlib.util.spec_from_file_location(name, _PATCH_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for var, p in prev.items():
            if p is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = p


@pytest.fixture()
def gmp(tmp_path):
    mod = _load_patch(f"gmp_vpace_{tmp_path.name}")
    mod._GT_BASELINE = False
    mod._ORACLE_ROUTE = True
    os.environ["GT_ORACLE_EVENTS"] = str(tmp_path / "ev.jsonl")
    return mod


def _drive(gmp, cmd, obs=""):
    out = {"output": obs, "returncode": 0}
    gmp._augment_output({"command": cmd}, out)


def test_never_test_agent_gets_edit_pace_v(gmp):
    """Edits at actions 2, 12, 22 (pace 10), zero test runs -> V = 10."""
    _drive(gmp, "cat src/a.py", "content")  # action 1
    _drive(gmp, "sed -i 's/a/b/' src/a.py")  # action 2  EDIT
    for i in range(9):
        _drive(gmp, f"grep -n thing src/f{i}.py", "hit")  # 3..11
    _drive(gmp, "sed -i 's/b/c/' src/a.py")  # action 12 EDIT
    for i in range(9):
        _drive(gmp, f"cat src/g{i}.py", "content")  # 13..21
    _drive(gmp, "sed -i 's/c/d/' src/b.py")  # action 22 EDIT
    assert gmp._test_cycle_spans == []  # truly never tested
    v = gmp._estimate_v()
    assert v == 10, f"never-test V must come from edit pace, got {v}"


def test_observed_test_spans_still_win(gmp):
    """EDIT->TEST spans, when they exist, dominate the edit-pace proxy."""
    gmp._test_cycle_spans.extend([15, 17, 19])
    gmp._edit_action_steps.extend([2, 4, 6, 8])  # pace 2 -> would rail to 8
    assert gmp._estimate_v() == 17


def test_single_edit_keeps_default(gmp):
    """One edit = no pace signal yet -> the default (25), not a guess."""
    _drive(gmp, "sed -i 's/a/b/' src/a.py")
    assert gmp._estimate_v() == 25


def test_edit_pace_is_railed(gmp):
    """Back-to-back edits (pace 1) rail to V_MIN; glacial pace rails to V_MAX."""
    for _ in range(5):
        _drive(gmp, "sed -i 's/a/b/' src/a.py")
    assert gmp._estimate_v() == gmp._V_MIN
    gmp._edit_action_steps.clear()
    gmp._edit_action_steps.extend([1, 100, 199])  # pace 99 -> rail 40
    assert gmp._estimate_v() == gmp._V_MAX
