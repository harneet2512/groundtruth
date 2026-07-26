"""The replay oracle must SAY which diffs are even comparable.

WHY (observability, NOT filtering).  A replay diff on a layer the recorded
baseline never emitted is not a fidelity failure -- it is incomparable data.
Measured 2026-07-26 across 5 held-out repos: **192 of 293 diffs (65.5%) sit on
layers with ZERO recorded rows**, because the recording (run 29236533134)
predates the ``control.participation`` and ``ss.coherence.proof`` lanes
entirely.  The oracle reports ``REPLAY_UNFAITHFUL`` without distinguishing the
two, so a reader cannot tell a stale baseline from a real regression without
re-deriving the analysis by hand.

THE LINE THIS MUST NOT CROSS.  Classification is REPORTING.  It must never
change a verdict, a count, or the exit code -- if it did, it would be exactly
the benchmaxxing the quality bar forbids: tuning a comparator until the gate
goes green.  A lane-aware FILTER would drop the 65.5% that is provably
incomparable and leave the other 34.5% just as uninterpretable, because a
replay against a stale recording cannot distinguish "changed because IMPROVED"
from "changed because BROKEN".  The fix for that is re-recording, not
filtering.  This only makes the existing verdict legible.

Same principle as the ``canonical_runtime.compilation`` ledger row added this
session: explain the outcome, change nothing about it.
"""

from __future__ import annotations

import pathlib
import sys


_SS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "swebench"
if str(_SS_DIR) not in sys.path:
    sys.path.insert(0, str(_SS_DIR))

import ss_replay_oracle as O  # noqa: E402


RECORDED = {"l3b.evidence": 10, "l3.contract": 17, "L6": 10}


def _diff(layer: str, op: str = "extra") -> dict:
    return {"op": op, "kind": "delivered", "iteration": 3, "layer": layer}


def test_oracle_exposes_a_diff_comparability_classifier() -> None:
    """RED until the oracle can say which diffs are comparable at all."""
    assert hasattr(O, "classify_diff_comparability"), (
        "the oracle reports REPLAY_UNFAITHFUL without distinguishing diffs on "
        "layers the baseline never emitted (65.5% of them, measured) from "
        "diffs on comparable layers"
    )


def test_layers_absent_from_the_recording_are_incomparable() -> None:
    diffs = [
        _diff("control.participation"),
        _diff("ss.coherence.proof"),
        _diff("l3b.evidence"),
    ]

    result = O.classify_diff_comparability(diffs, RECORDED)

    assert result["novel_lane"] == 2
    assert result["comparable"] == 1
    assert sorted(result["novel_layers"]) == [
        "control.participation", "ss.coherence.proof",
    ]


def test_all_comparable_when_every_layer_was_recorded() -> None:
    diffs = [_diff("l3b.evidence"), _diff("l3.contract", op="missing")]

    result = O.classify_diff_comparability(diffs, RECORDED)

    assert result["novel_lane"] == 0
    assert result["comparable"] == 2
    assert result["novel_layers"] == []


def test_a_recorded_layer_with_zero_count_is_still_incomparable() -> None:
    """A layer present as a key but never emitted is not comparable either.

    Biting: an implementation testing ``layer in recorded`` rather than
    ``recorded.get(layer, 0) > 0`` passes every other case and fails this one.
    """
    result = O.classify_diff_comparability(
        [_diff("detect.coherence")], {"detect.coherence": 0},
    )

    assert result["novel_lane"] == 1
    assert result["comparable"] == 0


def test_empty_diffs_are_reported_as_fully_comparable_not_as_an_error() -> None:
    """A faithful task has no diffs; that must not read as 100% artifact."""
    result = O.classify_diff_comparability([], RECORDED)

    assert result["novel_lane"] == 0
    assert result["comparable"] == 0
    assert result["artifact_fraction"] == 0.0


def test_artifact_fraction_matches_the_measured_population() -> None:
    """Sanity-check the ratio against the real 5-repo measurement."""
    diffs = [_diff("control.participation") for _ in range(192)]
    diffs += [_diff("l3b.evidence") for _ in range(101)]

    result = O.classify_diff_comparability(diffs, RECORDED)

    assert result["novel_lane"] == 192
    assert result["comparable"] == 101
    assert round(result["artifact_fraction"], 3) == 0.655
