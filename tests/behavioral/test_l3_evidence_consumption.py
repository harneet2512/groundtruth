from __future__ import annotations

import pytest

from tests.behavioral.utils import first_actions, iter_task_dirs, list_evidence_texts, load_trajectory, overlap_ratio, steps


@pytest.mark.behavioral
def test_l3_evidence_consumption(trajectory_dir, baselines):
    ratios = []
    for td in iter_task_dirs(trajectory_dir):
        tr = load_trajectory(td)
        st = steps(tr)
        ev = list_evidence_texts(td)
        if not ev or not st:
            continue
        nxt = "\n".join(first_actions(st, 3))
        ratios.append(overlap_ratio(ev[-1], nxt))
    if not ratios:
        pytest.skip("UNVERIFIED: no evidence chains available")
    observed = sum(ratios) / len(ratios)
    threshold = float(baselines.get("l3", {}).get("threshold", 0.05))
    assert observed >= threshold, f"L3 overlap {observed:.3f} < threshold {threshold:.3f}"
