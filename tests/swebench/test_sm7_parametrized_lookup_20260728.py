"""SM-7 could not verify a PARAMETRIZED firing test, and reported the member as non-firing.

`sm7_gate._pytest_one` runs FILES and keys its result map on the node-ids pytest prints with `-v`.
A parametrized test never prints its bare id — pytest emits
`...::test_safety_classes_never_held_out_at_any_rate[submit.refusal-0.999999]` and 41 siblings.
So the mapped BARE id was never a key, `_judge` reported MISSING, and `GT_SS_SHADOW` was graded a
FIRING FAILURE while its test was green.

WHY THIS MATTERS MORE THAN ONE MEMBER. A gate that reports RED for a member whose test passes is
not a conservative gate — it is a broken one, and it does the same damage as a false green: the
next person to see it learns to discount the gate. It also mislabels the finding: "member does not
fire" and "the harness cannot see this test shape" are different problems with different owners.

THE FIX IS IN THE LOOKUP, NOT THE TEST. The parametrization is what makes the safety-class claim
meaningful — it asserts the property across every class and every rate. Un-parametrizing it to
satisfy the harness would weaken a real invariant to make a tool happy.

FAIL-CLOSED IS PRESERVED. A bare id with NO parametrized children is still MISSING, and children
that are not all green still FAIL — the relaxation only recognises the id SHAPE, never a weaker
verdict.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — drop the parametrized-children branch: `test_a_fully_green_parametrized_test_is_a_pass`
       goes RED and GT_SS_SHADOW is a firing failure again.
  M2 — accept a bare id when ANY child is green: `test_one_red_parametrization_still_fails` goes
       RED, and a safety property that holds at one rate would pass for all of them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "scripts" / "swebench"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sm7_gate  # noqa: E402


_NODE = "tests/runtime/test_ss8_shadow_holdout.py::test_safety_classes_never_held_out_at_any_rate"


def test_the_probe_can_produce_a_pass() -> None:
    """CALIBRATION: a plain green id must still pass, or every assertion below is unreadable."""
    verdict, _ = sm7_gate._judge(["a.py::test_x"], {"a.py::test_x": "PASSED"})
    assert verdict == sm7_gate.PASS


def test_a_fully_green_parametrized_test_is_a_pass() -> None:
    """M1. The real GT_SS_SHADOW shape: 42 parametrized ids, no bare id in the results."""
    results = {
        f"{_NODE}[submit.refusal-{rate}]": "PASSED"
        for rate in ("0.0", "0.5", "0.999999")
    }
    verdict, detail = sm7_gate._judge([_NODE], results)
    assert verdict == sm7_gate.PASS, detail


def test_one_red_parametrization_still_fails() -> None:
    """M2. A safety property that holds at one rate must not pass for all of them."""
    results = {
        f"{_NODE}[submit.refusal-0.0]": "PASSED",
        f"{_NODE}[submit.refusal-0.999999]": "FAILED",
    }
    verdict, _ = sm7_gate._judge([_NODE], results)
    assert verdict == sm7_gate.FAIL


def test_a_bare_id_with_no_children_is_still_missing() -> None:
    """Fail-closed: recognising a SHAPE must not invent a result for a test that never ran."""
    verdict, detail = sm7_gate._judge([_NODE], {"other.py::test_y": "PASSED"})
    assert verdict == sm7_gate.FAIL
    assert "MISSING" in detail


def test_a_prefix_that_is_not_a_parametrization_is_not_a_child() -> None:
    """`test_foo` must not be satisfied by `test_foobar` — the bracket is the boundary."""
    results = {f"{_NODE}_extra": "PASSED"}
    verdict, detail = sm7_gate._judge([_NODE], results)
    assert verdict == sm7_gate.FAIL
    assert "MISSING" in detail
