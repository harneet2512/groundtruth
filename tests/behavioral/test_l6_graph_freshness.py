from __future__ import annotations

import pytest

from tests.behavioral.utils import iter_task_dirs, list_evidence_texts


@pytest.mark.behavioral
def test_l6_graph_freshness(trajectory_dir, graph_dir):
    _ = graph_dir
    eligible = 0
    changed = 0
    for td in iter_task_dirs(trajectory_dir):
        ev = list_evidence_texts(td)
        if len(ev) < 2:
            continue
        eligible += 1
        if ev[-1] != ev[-2]:
            changed += 1
    if eligible == 0:
        pytest.skip("UNVERIFIED: no eligible edit->evidence chains")
    assert changed >= 1, "No evidence-to-evidence freshness change observed"
