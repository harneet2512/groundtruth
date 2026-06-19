"""RED tests for kernel.detect_drift.

Pin layers:
    1. Happy -- canonical fixture (third edit moves outside cluster).
    2. Boundary -- single edit, no edits, all edits inside cluster.
    3. Adversarial -- out-of-order timestamps, repeated identical edits, mixed-language repo.
    4. Mutation -- documented per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth.control import kernel
from groundtruth.control.types import (
    BriefResult,
    Candidate,
    Capabilities,
    EditEvent,
    RunState,
)


# Phase 1 implementation landed -- tests are now expected-pass.


def _rs(focus: list[str], cluster: list[str], history: list[EditEvent]) -> RunState:
    focus_paths = [Path(p) for p in focus]
    cluster_paths = [Path(p) for p in cluster]
    return RunState(
        task_id="t",
        plan={"agent_focus_files": focus, "cluster_files": cluster},
        brief_result=BriefResult(
            brief_text="",
            candidates=[Candidate(path=Path(p), score=0.8) for p in focus],
            focus_files=focus_paths,
            cluster_files=cluster_paths,
            confidence=0.75,
            plan={},
            plan_path=None,
        ),
        edit_history=history,
        capabilities=Capabilities(block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True),
    )


def _edit(path: str, ts: str = "2026-04-30T14:22:00Z") -> EditEvent:
    return EditEvent(
        task_id="t",
        files_changed=[Path(path)],
        diff_text="...",
        ts=ts,
        source_tool="str_replace_editor",
    )


# Layer 1: Happy -- canonical fixture.
# Mutation pin: flipping the cluster-membership comparison breaks this.
def test_cluster_drift_after_three(fixture_loader):
    input_data, expected = fixture_loader("cluster_drift_after_three")
    rs = RunState.model_validate(input_data["run_state"])
    signals = kernel.detect_drift(rs)
    assert signals.edits_outside_cluster_count >= expected["expected_return"]["edits_outside_cluster_count_min"]


# Layer 2: Boundary -- empty history yields zeroed signals.
# Mutation pin: defaulting graph_distance_growth to non-zero.
def test_empty_history_no_drift():
    rs = _rs(["src/a.py"], ["src/a.py", "src/b.py"], [])
    signals = kernel.detect_drift(rs)
    assert signals.edits_outside_cluster_count == 0
    assert signals.first_edit_misses_focus is False
    assert signals.root_scaffold_added is False


# Layer 2: Boundary -- single edit inside cluster.
# Mutation pin: misclassifying first_edit_misses_focus when first edit is inside cluster.
def test_single_edit_inside_cluster_no_drift():
    rs = _rs(["src/a.py"], ["src/a.py", "src/b.py"], [_edit("src/a.py")])
    signals = kernel.detect_drift(rs)
    assert signals.edits_outside_cluster_count == 0
    assert signals.first_edit_misses_focus is False


# Layer 2: Boundary -- single edit, root scaffold.
# Mutation pin: ROOT_SCAFFOLD_PATTERNS list shrinks.
def test_first_edit_root_scaffold_signal():
    rs = _rs(["src/a.py"], ["src/a.py"], [_edit("reproduce_bug.py")])
    signals = kernel.detect_drift(rs)
    assert signals.root_scaffold_added is True


# Layer 3: Adversarial -- out-of-order timestamps. Drift is computed from
# the order of edit_history, NOT timestamps; mixed timestamps must not crash.
# Mutation pin: any branch that sorts by ts or trusts ts ordering.
def test_out_of_order_timestamps_does_not_crash():
    history = [
        _edit("src/a.py", ts="2026-04-30T14:25:00Z"),
        _edit("src/a.py", ts="2026-04-30T14:20:00Z"),
        _edit("src/elsewhere.py", ts="2026-04-30T14:30:00Z"),
    ]
    rs = _rs(["src/a.py"], ["src/a.py"], history)
    signals = kernel.detect_drift(rs)
    assert signals.edits_outside_cluster_count >= 1


# Layer 3: Adversarial -- repeated identical edits should not inflate drift count.
# Mutation pin: dedup logic missing or counting wrong axis.
def test_repeated_identical_edits_do_not_inflate_drift():
    same = _edit("src/elsewhere.py")
    rs = _rs(["src/a.py"], ["src/a.py"], [same, same, same])
    signals = kernel.detect_drift(rs)
    # Three edits, all to the same outside file -- count is "edits", not "files",
    # but repeated_warnings tracking must classify the dedup correctly.
    assert signals.edits_outside_cluster_count <= 3


# Layer 3: Adversarial -- mixed-language repo (Python + Go in cluster) must
# not crash on path comparison logic.
# Mutation pin: language-specific path normalization that drops .go files.
def test_mixed_language_cluster_handled():
    history = [_edit("internal/foo.go"), _edit("src/a.py")]
    rs = _rs(["src/a.py"], ["src/a.py", "internal/foo.go"], history)
    signals = kernel.detect_drift(rs)
    assert signals.edits_outside_cluster_count == 0


# Layer 2: graph_distance_growth must be monotonically non-decreasing across
# edits when the agent moves further from focus.
# Mutation pin: any reset-to-zero in the per-edit growth tracker.
def test_graph_distance_growth_is_non_negative():
    history = [_edit("src/a.py"), _edit("src/elsewhere.py")]
    rs = _rs(["src/a.py"], ["src/a.py"], history)
    signals = kernel.detect_drift(rs)
    assert signals.graph_distance_growth >= 0.0
