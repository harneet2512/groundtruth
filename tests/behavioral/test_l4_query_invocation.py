from __future__ import annotations

import pytest

from tests.behavioral.utils import count_gt_query_calls, iter_task_dirs, load_trajectory, steps


@pytest.mark.behavioral
def test_l4_query_invocation(trajectory_dir):
    counts = []
    for td in iter_task_dirs(trajectory_dir):
        tr = load_trajectory(td)
        if not steps(tr):
            continue
        counts.append(count_gt_query_calls(td, tr))
    if not counts:
        pytest.skip("UNVERIFIED: no trajectories")
    avg = sum(counts) / len(counts)
    assert avg >= 1.0, f"avg gt_query invocations/task {avg:.2f} < 1.0"
