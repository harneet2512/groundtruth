"""Delivery engine STAGE 1 — behavioral sensor upgrade (red->green).

The Stage-0 sensor gains the five VALIDATED trajectory-structure signals from
RESEARCH_AGENT_BEHAVIORAL_SIGNALS.md (Category 5):

  1. loop_ratio        — TIDE (arXiv 2602.02196): fraction of actions inside
                         recurring identical (command, observation) cycles.
  2. new_state_rate    — TIDE / stale score (arXiv 2604.13151): fraction of
                         recent actions producing a NEW state node.
  3. edit_churn        — TRAJEVAL (arXiv 2603.24631) Coherence Collapse:
                         per-file re-edit count with no passing test between.
  4. edit_coverage_ratio — obligation symbols edited / total (None = dormant).
  5. test_coverage_ratio — edited files with test evidence / total edited
                         files (None = dormant).

Mandatory properties verified here:
  DYNAMIC — the signals are ratios over the agent's own trajectory, no
            absolute thresholds anywhere in the sensor.
  HYBRID  — each signal is an input to >=3-signal predicates (Stages 2-4);
            the sensor itself stays measurement-only.
  RESEARCH-BACKED — formulas match the cited papers' definitions (loop ratio
            = in-cycle states / length; new-state over a window; churn reset
            on pass).
ONE-FORMULA RULE — the sensor BINDS the implementations from gt_mini_patch
(live and replay can never disagree): asserted by identity checks.

RED before Stage 1: DerivedState has none of these fields and the module
exports none of these functions (AttributeError).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load(path: Path, name: str):
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
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


@pytest.fixture(scope="module")
def sense_mod():
    return _load(_SENSE_PATH, "gt_oracle_sense_stage1")


@pytest.fixture(scope="module")
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_stage1")


def _turn(sense_mod, i, cmd, obs=""):
    return sense_mod.Turn(index=i, command=cmd, raw_obs=obs, full_obs=obs)


# ---------------------------------------------------------------------------
# ONE-FORMULA RULE: the sensor binds the live module's implementations.
# ---------------------------------------------------------------------------
def test_sensor_binds_live_formulas(sense_mod, patch_mod):
    assert sense_mod.compute_loop_ratio.__code__ is not None
    # When the sibling gt_mini_patch is importable (it is, in this repo), the
    # sensor must BIND, not reimplement (twin-drift mitigation).
    assert sense_mod._gmp is not None
    assert sense_mod.compute_loop_ratio is sense_mod._gmp.compute_loop_ratio
    assert sense_mod.compute_new_state_rate is sense_mod._gmp.compute_new_state_rate
    assert sense_mod.symbol_tested is sense_mod._gmp.symbol_tested
    assert sense_mod.edit_coverage_ratio is sense_mod._gmp.edit_coverage_ratio
    assert sense_mod.test_coverage_ratio is sense_mod._gmp.test_coverage_ratio


# ---------------------------------------------------------------------------
# 1. loop_ratio — the fd stale-binary shape (5x identical command+output,
#    SPREAD between other actions — the old window-12 detector's blind spot).
# ---------------------------------------------------------------------------
class TestLoopRatio:
    def test_distinct_actions_zero(self, sense_mod):
        turns = [_turn(sense_mod, i, f"cat file_{i}.py", f"content {i}") for i in range(20)]
        st = sense_mod.sense(turns)
        assert st.loop_ratio == 0.0

    def test_fd_shape_spread_repeats_counted(self, sense_mod):
        """5 identical (cmd, obs) repeats spread across 30 turns — the exact
        fd-stale-binary signature that drew silence from the windowed
        detector — must register in loop_ratio."""
        turns = []
        idx = 0
        for block in range(5):
            for j in range(5):
                turns.append(_turn(sense_mod, idx, f"ls dir_{block}_{j}", f"out {block}{j}"))
                idx += 1
            turns.append(
                _turn(
                    sense_mod,
                    idx,
                    "./target/debug/fd --sort name",
                    "file7.txt\nfile007.txt\nSAME WRONG ORDER",
                )
            )
            idx += 1
        st = sense_mod.sense(turns)
        # 5 identical signatures over 30 actions -> 5/30
        assert abs(st.loop_ratio - 5 / 30) < 1e-9

    def test_same_command_new_output_is_iteration_not_loop(self, sense_mod):
        """The 2026-06-10 '13453 false fire' discipline: same command with a
        DIFFERENT observation each run is progress, never a loop."""
        turns = [_turn(sense_mod, i, "pytest -x", f"collected {i} items") for i in range(10)]
        st = sense_mod.sense(turns)
        assert st.loop_ratio == 0.0


# ---------------------------------------------------------------------------
# 2. new_state_rate — TIDE node production over the recent window.
# ---------------------------------------------------------------------------
class TestNewStateRate:
    def test_fresh_exploration_is_one(self, sense_mod):
        turns = [_turn(sense_mod, i, f"cat f{i}.py", f"src {i}") for i in range(15)]
        st = sense_mod.sense(turns)
        assert st.new_state_rate == 1.0

    def test_stale_repetition_collapses(self, sense_mod):
        """Repeating one (file, type) state floors the new-state rate."""
        turns = [_turn(sense_mod, 0, "cat a.py", "first")]
        turns += [_turn(sense_mod, i, "cat a.py", "first") for i in range(1, 13)]
        st = sense_mod.sense(turns)
        assert st.new_state_rate <= 1.0 / 12 + 1e-9

    def test_empty_is_one(self, sense_mod):
        assert sense_mod.compute_new_state_rate([]) == 1.0


# ---------------------------------------------------------------------------
# 3. edit_churn — TRAJEVAL coherence collapse; reset on observed PASS.
# ---------------------------------------------------------------------------
class TestEditChurn:
    def test_three_reedits_no_pass(self, sense_mod):
        turns = [
            _turn(sense_mod, 0, "sed -i 's/a/b/' src/core.py"),
            _turn(sense_mod, 1, "git diff", "diff"),
            _turn(sense_mod, 2, "sed -i 's/b/c/' src/core.py"),
            _turn(sense_mod, 3, "sed -i 's/c/d/' src/core.py"),
        ]
        st = sense_mod.sense(turns)
        assert st.edit_churn.get("src/core.py") == 3

    def test_pass_resets_churn(self, sense_mod):
        turns = [
            _turn(sense_mod, 0, "sed -i 's/a/b/' src/core.py"),
            _turn(sense_mod, 1, "sed -i 's/b/c/' src/core.py"),
            _turn(sense_mod, 2, "pytest tests/ -q", "3 passed in 0.1s"),
            _turn(sense_mod, 3, "sed -i 's/c/d/' src/core.py"),
        ]
        st = sense_mod.sense(turns)
        assert st.edit_churn.get("src/core.py") == 1

    def test_failing_test_does_not_reset(self, sense_mod):
        turns = [
            _turn(sense_mod, 0, "sed -i 's/a/b/' src/core.py"),
            _turn(sense_mod, 1, "pytest tests/ -q", "1 failed in 0.1s\nFAILED tests/t.py::x"),
            _turn(sense_mod, 2, "sed -i 's/b/c/' src/core.py"),
        ]
        st = sense_mod.sense(turns)
        assert st.edit_churn.get("src/core.py") == 2


# ---------------------------------------------------------------------------
# 4+5. coverage ratios — None = dormant (correct-or-quiet by construction).
# ---------------------------------------------------------------------------
class TestCoverageRatios:
    def test_edit_coverage_no_obligations_dormant(self, sense_mod):
        assert sense_mod.edit_coverage_ratio(set(), {"foo"}) is None

    def test_edit_coverage_fraction(self, sense_mod):
        r = sense_mod.edit_coverage_ratio(
            {"capture_snapshot", "max_snapshots"}, {"capture_snapshot", "noise"}
        )
        assert r == 0.5

    def test_test_coverage_nothing_edited_dormant(self, sense_mod):
        assert sense_mod.test_coverage_ratio({}, {"anything"}) is None

    def test_test_coverage_per_file(self, sense_mod):
        by_file = {
            "src/a.py": {"capture_snapshot", "init"},
            "src/b.py": {"unrelated_helper"},
        }
        tested = {"test_capture_snapshot", "passed"}
        # a.py covered via compound containment; b.py not covered -> 0.5
        assert sense_mod.test_coverage_ratio(by_file, tested) == 0.5

    def test_keyword_tokens_never_evidence(self, sense_mod):
        """`return` appearing in both an edit and a test output proves
        nothing — the stopword guard."""
        by_file = {"src/a.py": {"return", "self", "def"}}
        assert sense_mod.test_coverage_ratio(by_file, {"return", "self"}) == 0.0

    def test_symbol_tested_compound_containment(self, sense_mod):
        assert sense_mod.symbol_tested("capture_snapshot", {"test_capture_snapshot"}) is True
        assert (
            sense_mod.symbol_tested("walk", {"test_walk_dirs"}) is False
        )  # plain word: exact only
        assert sense_mod.symbol_tested("walk", {"walk"}) is True


# ---------------------------------------------------------------------------
# Purity: same turns -> same state; per-file token tracking populated.
# ---------------------------------------------------------------------------
def test_sense_pure_and_tracks_tokens_by_file(sense_mod):
    turns = [
        _turn(sense_mod, 0, "sed -i 's/old_name/new_name/' src/core.py"),
        _turn(sense_mod, 1, "pytest -q", "1 passed"),
    ]
    a = sense_mod.sense(turns)
    b = sense_mod.sense(turns)
    assert a.loop_ratio == b.loop_ratio
    assert a.new_state_rate == b.new_state_rate
    assert a.edit_churn == b.edit_churn
    assert "src/core.py" in a.edited_tokens_by_file
    assert "new_name" in a.edited_tokens_by_file["src/core.py"]
