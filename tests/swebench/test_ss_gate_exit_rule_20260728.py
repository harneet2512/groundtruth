"""The acceptance gate must not report RED on a strictly better result.

MEASURED 2026-07-28.  ``ss_gate._exit_code`` required ``expected_verdicts = {"S8": SKIP}``
and then an exact ``counts == {PASS: 11, FAIL: 0, SKIP: 1, ERROR: 0}``.

S8 (EMPTY-PAYLOAD) is flag-gated.  It SKIPPED for as long as its flag was not built, and
the gate's success rule was written around that transient state.  Once the flag was built
S8 began to PASS, and the gate went RED on::

    PASS=12 FAIL=0 SKIP=0 ERROR=0 / 12 scenarios
    EXIT 1 (RED)

i.e. every scenario green was classified as failure, while 11 PASS + 1 SKIP was the only
accepted outcome.  A gate that cannot recognise improvement is not a gate -- and this one
is the Wave 1 exit criterion, so it would have blocked the wave on a perfect run.

This is the mirror image of the "substrate gate = TAUTOLOGICAL" class already on record.
There a release gate could never FAIL; here an acceptance gate could never SUCCEED once
the thing it measures got better.  Both come from encoding a MOMENT rather than a
PROPERTY.

The property: every scenario present, nothing FAILED, nothing ERRORed, and no scenario
silently skipped except the one whose flag may legitimately be absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

import ss_gate  # noqa: E402


def _results(overrides=None):
    """S0..S11, all PASS unless overridden."""
    overrides = overrides or {}
    return [
        ss_gate.ScenarioResult(
            sid=f"S{i}", name=f"scenario-{i}", flag="",
            verdict=overrides.get(f"S{i}", ss_gate.PASS), detail="", subchecks=[],
        )
        for i in range(12)
    ]


def test_all_twelve_passing_is_GREEN():
    """THE BUG. This exact shape was reported RED."""
    assert ss_gate._exit_code(_results()) == 0


def test_s8_skipping_is_still_GREEN():
    """BACKWARD COMPATIBILITY. S8's flag may legitimately not be built."""
    assert ss_gate._exit_code(_results({"S8": ss_gate.SKIP})) == 0


def test_any_FAIL_is_RED():
    assert ss_gate._exit_code(_results({"S3": ss_gate.FAIL})) == 1


def test_any_ERROR_is_RED():
    assert ss_gate._exit_code(_results({"S5": ss_gate.ERROR})) == 1


def test_a_NON_s8_skip_is_RED():
    """ANTI-WEAKENING. Only S8 may skip -- any other silent skip hides a scenario that
    never ran, which is the failure mode this gate exists to prevent."""
    assert ss_gate._exit_code(_results({"S3": ss_gate.SKIP})) == 1


def test_a_missing_scenario_is_RED():
    """ANTI-WEAKENING. Eleven green scenarios are not a pass when twelve were expected."""
    assert ss_gate._exit_code(_results()[:-1]) == 1


def test_scenarios_out_of_order_is_RED():
    """The id sequence is the manifest; a reordered run is not the declared gate."""
    r = _results()
    r[0], r[1] = r[1], r[0]
    assert ss_gate._exit_code(r) == 1
