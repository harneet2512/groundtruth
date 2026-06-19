from __future__ import annotations

import pytest

from tests.behavioral.utils import iter_task_dirs, load_trajectory


@pytest.mark.behavioral
def test_l2_fallback_orientation(trajectory_dir):
    # L2 eligibility is detected from the on-disk artifacts the layer
    # actually wrote, not from a `info.identifier_count` field that the
    # hook does not populate. A task is L2-eligible iff its
    # `gt_layers.log` records `L2=fired` or `L2=fired_but_empty`, OR
    # the brief itself carries the L2 wrapper or sparse sentinel.
    eligible = []
    for td in iter_task_dirs(trajectory_dir):
        layers_log = td / "gt_layers.log"
        l2_status = ""
        if layers_log.exists():
            txt = layers_log.read_text(encoding="utf-8", errors="replace")
            for line in txt.splitlines():
                if "L2=" in line:
                    for tok in line.split():
                        if tok.startswith("L2="):
                            l2_status = tok.split("=", 1)[1]
                            break
                    if l2_status:
                        break
        brief_path = td / "gt_brief.txt"
        brief_txt = brief_path.read_text(encoding="utf-8", errors="replace") if brief_path.exists() else ""
        l2_fired = l2_status.startswith("fired") or "<gt-task-brief>" in brief_txt or "STRUCTURAL RETRIEVAL" in brief_txt
        if l2_fired:
            eligible.append((td, brief_txt))
    if not eligible:
        pytest.skip("UNVERIFIED: no L2-fired tasks present")
    for td, brief in eligible:
        if not brief:
            pytest.fail(f"{td.name}: L2 fired but gt_brief.txt is empty/missing")
        assert (
            "STRUCTURAL RETRIEVAL" in brief
            or "BM25" in brief
            or "fallback" in brief.lower()
            or "<gt-task-brief>" in brief
        ), f"{td.name}: L2-fired brief missing expected fallback marker"
