"""F3 (Fable 2026-07-05): _reset_oracle_state is a TEST-ISOLATION helper (7+ suites call it), so
every producer/latch module-global it MISSES leaks "already fired" state into the next case and
silently reddens/greens the steer under test.

RED on the pre-fix reset: these 10 globals keep their fired/non-zero value after the reset.
"""
from __future__ import annotations

import gt_mini_patch as g


def test_reset_clears_previously_missed_latches():
    # Fire every previously-missed latch/counter to a non-default value.
    g._source_edit_count = 7
    g._oracle_review_fired = True
    g._oblig_final_shot_fired = True
    g._l5_finish_fired = True
    g._l5_failure_fired = True
    g._l5_notest_fired = True
    g._marker_sent = True
    g._horizon_urgent_fired = True
    g._horizon_pivot_fired = True
    g._horizon_gate_fire_count = 5

    g._reset_oracle_state()

    # All must return to their fresh-slate defaults (RED pre-fix: they stay set).
    assert g._source_edit_count == 0
    assert g._oracle_review_fired is False
    assert g._oblig_final_shot_fired is False
    assert g._l5_finish_fired is False
    assert g._l5_failure_fired is False
    assert g._l5_notest_fired is False
    assert g._marker_sent is False
    assert g._horizon_urgent_fired is False
    assert g._horizon_pivot_fired is False
    assert g._horizon_gate_fire_count == 0


def test_reset_still_clears_the_original_core_latches():
    # Guard against a regression that drops a pre-existing reset while adding the new ones.
    g._consensus_fired = True
    g._cochange_fired = True
    g._l5_fired = True
    g._oracle_nonedit_streak = 9
    g._action_count = 42
    g._reset_oracle_state()
    assert g._consensus_fired is False
    assert g._cochange_fired is False
    assert g._l5_fired is False
    assert g._oracle_nonedit_streak == 0
    assert g._action_count == 0
