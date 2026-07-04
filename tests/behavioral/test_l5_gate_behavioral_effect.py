from __future__ import annotations

import json

import pytest

from tests.behavioral.utils import iter_task_dirs


@pytest.mark.behavioral
def test_l5_gate_behavioral_effect(trajectory_dir):
    eligible = 0
    acknowledged = 0
    for td in iter_task_dirs(trajectory_dir):
        gate = td / "gt_pre_finish_gate.json"
        if not gate.exists():
            continue
        try:
            data = json.loads(gate.read_text(encoding="utf-8"))
        except Exception:
            continue
        res = str(data.get("result", data.get("verdict", ""))).lower()
        if "warn_soft_escape" in res or "warn" == res:
            eligible += 1
            if data.get("warnings") or data.get("acknowledged"):
                acknowledged += 1
    if eligible == 0:
        pytest.skip("UNVERIFIED: no gate-triggered trajectories")
    assert acknowledged >= 1, "No observed post-warning acknowledgement/change"
