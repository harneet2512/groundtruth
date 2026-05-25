from __future__ import annotations

import pytest

from tests.behavioral.utils import collect_file_mentions, find_brief_text, first_actions, iter_task_dirs, load_trajectory, steps


@pytest.mark.behavioral
def test_l1_navigation_impact(trajectory_dir, baselines):
    task_dirs = iter_task_dirs(trajectory_dir)
    measurable = 0
    hits = 0
    for td in task_dirs:
        tr = load_trajectory(td)
        st = steps(tr)
        if not st:
            continue
        brief = find_brief_text(td, tr)
        if not brief.strip():
            pytest.fail(f"{td.name}: missing non-empty brief signal")
        # Brief is substantive iff it carries at least one production marker.
        # This list mirrors `_L1_SUBSTANTIVE_MARKERS` in
        # scripts/swebench/gt_track4_pre_run.py and the wrappers used by
        # gt_intel.format_gt_output (`<gt-evidence>`) and the L2 fallback
        # (`<gt-task-brief>`). If a marker is renamed there, update here too.
        SUBSTANTIVE_MARKERS = (
            "<gt-task-brief>", "<gt-evidence>",
            "FIX HERE", "[VERIFIED]", "[LIKELY]", "[POSSIBLE]",
            "CALLERS:", "TEST:", "ENTRY POINT:",
            "CALLER-BLIND-EDIT", "HALLUCINATED-IMPORT",
            "PATTERN-DIVERGENCE", "UNVERIFIED-EDIT",
            "BLAST-RADIUS", "CONTRACT-BREAK", "STYLE-DIVERGENCE",
            "STRUCTURAL RETRIEVAL",
        )
        assert any(m in brief for m in SUBSTANTIVE_MARKERS), (
            f"{td.name}: brief has no substantive marker"
        )
        # The L2 sparse-retrieval sentinel intentionally cites no files
        # (`Issue text too sparse for structural retrieval. Agent should
        # rely on its own exploration of the codebase.`). That is the
        # layer telling the agent it has nothing to localize — there is
        # nothing for L1 navigation to score on this task. Skip it.
        if "Issue text too sparse for structural retrieval" in brief:
            continue
        brief_files = collect_file_mentions(brief)
        assert brief_files, f"{td.name}: no file paths mentioned in brief"
        a = "\n".join(first_actions(st, 3))
        measurable += 1
        if any(f in a for f in brief_files):
            hits += 1
    if measurable == 0:
        pytest.skip("UNVERIFIED: no measurable trajectories")
    observed = hits / measurable
    threshold = float(baselines.get("l1", {}).get("effective_threshold", 0.10))
    assert observed >= threshold, f"L1 hit rate {observed:.3f} < threshold {threshold:.3f}"
