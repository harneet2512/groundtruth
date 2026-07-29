#!/usr/bin/env python3
"""B-J3 TTD — unit tests for the chronology_extract reader field-priority + coarse->fine
normalization. Red before the fix, green after. Includes biting mutations.

Run: PYTHONPATH=scripts/swebench:src pytest test_bj3_reader.py -q
"""
import sys
from pathlib import Path

# scripts/swebench is not a package and is not installed; the module is importable only when
# that directory is on sys.path. The `Run:` line above sets PYTHONPATH, but a bare
# `pytest <this file>` (or any isolated collection) does not, so the file used to be a
# collection ERROR whenever it ran alone. Same bootstrap as the working neighbours
# (tests/swebench/test_gt_feature_metrics_128.py).
for _p in (Path(__file__).resolve().parents[2] / "scripts" / "swebench",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import chronology_extract as ce  # noqa: E402
from groundtruth.runtime.fact_registry import EVENTS, required_event  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. field priority: a present+registered actual_event WINS over the coarse event_type
# --------------------------------------------------------------------------- #
def test_actual_event_preferred_over_event_type():
    # covering_red real shape: coarse hook phase in event_type, fine event in actual_event.
    row = {"event_type": "post_edit", "actual_event": "test_result"}
    assert ce._row_actual_event(row) == "test_result"


def test_syntax_result_shape_prefers_fine():
    row = {"event_type": "post_edit", "actual_event": "edit_result"}
    assert ce._row_actual_event(row) == "edit_result"


def test_recovery_shape_prefers_failure_obs_not_test_result():
    # recovery: event_type is the surrounding test observation; the fine event is failure_obs.
    row = {"event_type": "test_result", "actual_event": "failure_obs"}
    assert ce._row_actual_event(row) == "failure_obs"
    assert ce._row_actual_event(row) == required_event("recovery")


# --------------------------------------------------------------------------- #
# 2. fallback: actual_event absent/empty -> event_type (UNNORMALIZED, legacy behavior)
# --------------------------------------------------------------------------- #
def test_fallback_to_event_type_when_actual_absent():
    assert ce._row_actual_event({"event_type": "post_view"}) == "post_view"


def test_fallback_to_event_type_when_actual_empty_string():
    assert ce._row_actual_event({"event_type": "post_edit", "actual_event": "  "}) == "post_edit"


def test_fallback_empty_when_neither_present():
    assert ce._row_actual_event({}) == ""


# --------------------------------------------------------------------------- #
# 3. coarse->fine normalization (search family drift + the unambiguous hook phases)
# --------------------------------------------------------------------------- #
def test_search_normalizes_to_search_result():
    # def_ref_partition / name_fold / wrong_surface stamp actual_event='search'.
    row = {"event_type": "", "actual_event": "search"}
    assert ce._row_actual_event(row) == "search_result"
    assert ce._row_actual_event(row) == required_event("def_ref_partition")


def test_post_view_normalizes_to_file_view():
    row = {"actual_event": "post_view"}
    assert ce._row_actual_event(row) == "file_view"


def test_all_normalized_targets_are_in_vocab():
    for coarse in ("search", "post_search", "view", "post_view",
                   "edit", "post_edit", "test", "post_test"):
        fine = ce._row_actual_event({"actual_event": coarse})
        assert fine in EVENTS, (coarse, fine)


# --------------------------------------------------------------------------- #
# 4. fail-closed: an unknown/unregistered token must stay unknown (never coerced to a pass)
# --------------------------------------------------------------------------- #
def test_unknown_token_left_verbatim_and_out_of_vocab():
    # 'review_transition' has no fine registry event -> must NOT be normalized.
    assert ce._row_actual_event({"actual_event": "review_transition"}) == "review_transition"
    assert "review_transition" not in EVENTS


def test_already_fine_token_passes_through_unchanged():
    for fine in ("test_result", "edit_result", "failure_obs", "submit",
                 "search_result", "file_view"):
        assert ce._row_actual_event({"actual_event": fine}) == fine


# --------------------------------------------------------------------------- #
# 5. BITING MUTATIONS — these must FAIL if the fix is reverted / weakened.
# --------------------------------------------------------------------------- #
def test_mutation_inverting_priority_would_fail():
    # If someone reverts to preferring event_type, covering_red reads 'post_edit' (out of
    # vocab). This asserts the fine event wins -> catches the inversion.
    assert ce._row_actual_event({"event_type": "post_edit",
                                 "actual_event": "test_result"}) == "test_result"
    assert ce._row_actual_event({"event_type": "post_edit",
                                 "actual_event": "test_result"}) in EVENTS


def test_mutation_normalizing_unknown_would_break_failclosed():
    # If normalization were fuzzy (mapped unknown tokens to some fine event), an
    # unregistered token would spuriously enter the vocab. Guard the fail-closed contract.
    out = ce._row_actual_event({"actual_event": "totally_unknown_phase"})
    assert out == "totally_unknown_phase"
    assert out not in EVENTS
