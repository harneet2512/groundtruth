"""The failure-SET baseline must be real, both-directional, and count-proof.

The pre-dispatch audit's "seam failure-SET vs baseline" leg had NO executable and NO stored set —
the baselines lived in session prose. This pins the replacement.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — return only `new_failures` (drop `unexpected_passes`): a stale baseline reads as a pass and
       `test_an_unexpectedly_passing_test_is_a_finding` goes RED.
  M2 — compare counts instead of sets: `test_a_swap_is_caught_where_a_count_would_not_be` goes
       RED, which is the whole reason this is by name.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "scripts" / "swebench"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import baseline_failure_set as bfs  # noqa: E402


def test_the_recorded_suites_and_sizes() -> None:
    """CALIBRATION. An empty baseline would make every diff below trivially clean."""
    assert set(bfs.BASELINE) == {
        "tests/runtime", "tests/swebench", "tests/pretask", "artifact_deepswe",
    }
    # artifact_deepswe went 12 -> 11 on 2026-07-28. NOT a fix and NOT a regression: the removed
    # entry (test_sm10_recovery_timing::test_recovery_candidate_producer) was never a real
    # failure. It failed only under a leaked `GT_GLOBAL_ARBITER=1`, written raw by
    # test_p10_premature_reactive_deferral's setup_module whose teardown popped a different
    # variable. Proven both ways: PASSES in isolation, FAILS under GT_GLOBAL_ARBITER=1. The
    # baseline had frozen a cross-module contamination artifact as a known defect.
    #
    # tests/pretask went 13 -> 8 on 2026-07-29, and this one IS a fix. The five
    # test_c1_passage_window_status nodes failed with AttributeError, not an assertion:
    # `embed.passage_window_status` existed at 439c55e7a and was dropped by the code-only
    # snapshot 3b3363d37 while its witness test and every helper it calls survived. The
    # function is restored and all five pass, so keeping them recorded would freeze a
    # SOURCE DELETION as a known red — the exact stale baseline this module warns about.
    assert {k: len(v) for k, v in bfs.BASELINE.items()} == {
        "tests/runtime": 2, "tests/swebench": 2,
        "tests/pretask": 8, "artifact_deepswe": 11,
    }


def test_every_recorded_name_points_at_a_file_that_exists() -> None:
    """A baseline naming a deleted test would go green forever without anyone noticing."""
    for suite, names in bfs.BASELINE.items():
        for node in names:
            path = node.split("::", 1)[0]
            assert (_ROOT / path).is_file(), f"{suite}: {path} does not exist"


def test_baseline_run_is_clean_in_both_directions() -> None:
    output = "\n".join(f"FAILED {n} - AssertionError" for n in bfs.BASELINE["tests/runtime"])
    result = bfs.diff("tests/runtime", bfs.parse_failures(output))
    assert result["new_failures"] == []
    assert result["unexpected_passes"] == []
    assert len(result["matched"]) == 2


def test_a_new_failure_is_caught() -> None:
    output = "FAILED tests/runtime/test_new.py::test_regression - AssertionError"
    result = bfs.diff("tests/runtime", bfs.parse_failures(output))
    assert result["new_failures"] == ["tests/runtime/test_new.py::test_regression"]


def test_an_unexpectedly_passing_test_is_a_finding() -> None:
    """M1. A stale baseline is not a pass — it means nobody knows what red means any more."""
    result = bfs.diff("tests/runtime", frozenset())
    assert len(result["unexpected_passes"]) == 2
    assert result["new_failures"] == []


def test_a_swap_is_caught_where_a_count_would_not_be() -> None:
    """M2. THE REASON THIS IS BY NAME. One test starts failing, another starts passing: the
    count is unchanged at 2 and both events are invisible to a count check."""
    kept = sorted(bfs.BASELINE["tests/runtime"])[0]
    observed = {kept, "tests/runtime/test_other.py::test_newly_red"}
    result = bfs.diff("tests/runtime", observed)
    assert len(observed) == len(bfs.BASELINE["tests/runtime"])  # the count matches
    assert result["new_failures"] == ["tests/runtime/test_other.py::test_newly_red"]
    assert len(result["unexpected_passes"]) == 1


def test_repeated_phases_of_one_test_are_one_failure() -> None:
    """pytest prints a name once per failing phase; three lines are not three regressions."""
    node = "tests/runtime/test_x.py::test_y"
    parsed = bfs.parse_failures(f"FAILED {node}\nFAILED {node} - boom\nERROR {node}")
    assert parsed == frozenset({node})


def test_non_summary_lines_are_ignored() -> None:
    noise = "2 failed, 2494 passed\n= FAILURES =\nsome FAILED text mid-line\n"
    assert bfs.parse_failures(noise) == frozenset()
