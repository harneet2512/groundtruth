"""FIX 1 (2026-06-11 measurement-gate closeout) — retire `scaffold_trap` on the
ORACLE route (red->green).

Research basis (gt_gt §16.4 + DEEP_TRAJECTORY_ANALYSIS): the early-patch-intensity
correlation is NEGATIVE (rho = -0.78) — penalizing exploration volume is wrong.
Live receipts: scaffold_trap fired uniformly at ~step 25 on 7/9 oracle-run tasks,
0 consumed, 1 confirmed WRONG steer (csstree -> fixture.js). On the oracle route
the arm is retired (scaffold_arm=False), exactly like the already-retired window
loop arm (loop_arm=False, Stage 3). The LEGACY route (GT_ORACLE_ROUTE=0) keeps
both arms unchanged (backward compat — byte-parity with pre-oracle behavior).

Red->green: on the oracle route, 26 actions with zero source edits must NOT
produce a scaffold_trap nudge; on the legacy route the same drive still does.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load_patch(name: str, tmp_path: Path):
    """Fresh module instance (clean per-test latches/counters)."""
    prev = os.environ.get("GT_BASELINE")
    prev_ev = os.environ.get("GT_ORACLE_EVENTS")
    os.environ["GT_BASELINE"] = "1"
    os.environ["GT_ORACLE_EVENTS"] = str(tmp_path / "gt_oracle_events.jsonl")
    try:
        spec = importlib.util.spec_from_file_location(name, _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev
        if prev_ev is None:
            os.environ.pop("GT_ORACLE_EVENTS", None)
        else:
            os.environ["GT_ORACLE_EVENTS"] = prev_ev


def _drive_nonedit_actions(mod, n: int) -> list[str]:
    """Drive n DISTINCT non-edit commands through _augment_output; return the
    appended GT text per action (distinct commands -> the loop arm never has a
    repeated signature; only scaffold_trap could fire)."""
    appended: list[str] = []
    for i in range(n):
        out = {"output": f"listing {i}"}
        mod._augment_output({"command": f"ls -la dir_{i}"}, out)
        appended.append(out["output"][len(f"listing {i}") :])
    return appended


# ---------------------------------------------------------------------------
# ORACLE route: scaffold_trap is RETIRED — 26 actions, 0 edits, NO fire.
# ---------------------------------------------------------------------------
def test_oracle_route_scaffold_trap_does_not_fire(tmp_path):
    mod = _load_patch("gmp_scaffold_oracle", tmp_path)
    mod._GT_BASELINE = False
    mod._ORACLE_ROUTE = True
    appended = _drive_nonedit_actions(mod, 30)
    joined = "".join(appended)
    assert mod._action_count >= 25
    assert mod._source_edit_count == 0
    assert "scaffold_trap" not in joined, (
        "scaffold_trap fired on the oracle route (retired arm): " + joined[-400:]
    )


# ---------------------------------------------------------------------------
# LEGACY route: backward compat — the arm still fires at >=25 / 0 edits.
# ---------------------------------------------------------------------------
def test_legacy_route_scaffold_trap_still_fires(tmp_path):
    mod = _load_patch("gmp_scaffold_legacy", tmp_path)
    mod._GT_BASELINE = False
    mod._ORACLE_ROUTE = False
    appended = _drive_nonedit_actions(mod, 30)
    joined = "".join(appended)
    assert "scaffold_trap" in joined, "legacy scaffold arm must be unchanged"


# ---------------------------------------------------------------------------
# Unit: the new scaffold_arm parameter gates ONLY the scaffold branch.
# ---------------------------------------------------------------------------
def test_l5_nudge_scaffold_arm_parameter(tmp_path):
    mod = _load_patch("gmp_scaffold_unit", tmp_path)
    mod._GT_BASELINE = False
    mod._action_count = 30
    mod._source_edit_count = 0

    mod._l5_fired = False
    assert mod._l5_nudge("echo hi", "", loop_arm=False, scaffold_arm=False) == ""
    assert mod._l5_fired is False, "retired arm must not consume the latch"

    mod._l5_fired = False
    fired = mod._l5_nudge("echo hi", "", loop_arm=False, scaffold_arm=True)
    assert "scaffold_trap" in fired, "scaffold_arm=True must keep prior behavior"
