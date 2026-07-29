"""GT's timing verdict must be able to say EARLY, and must split LATE.

WHY THIS IS THE SMALLEST HIGH-VALUE RED IN THE PROGRAM.  Half the 17-capability
mission is per-feature TIMING, and today the vocabulary cannot express it.
``_timing`` (chronological_adjudication.py:113-166) yields only
``ON_TIME | LATE | STEP_BEHIND | WRONG_EVENT | UNMEASURED``:

* **EARLY is swallowed.**  ``delivered < opened`` returns ``UNMEASURED``
  (:145-146) — "we could not measure it" — when in fact it is the most precisely
  measured thing available: the bytes landed before the decision they answer was
  even open.
* **LATE fuses two opposite outcomes.**  ``delivered > committed`` (:164-165) is
  returned whether or not the agent still had a decision left to change.  Evidence
  arriving after a commit that gets REVISED is corrective and valuable; evidence
  arriving after the last commit is expired and worthless.  One string, two
  meanings, no way to tell them apart — the same defect class as the ``acted``
  receipt field that was demoted earlier in this program.

This file changes NO product bytes.  It widens an offline adjudicator only, which
is deliberate: the enforcement order is RECORD BEFORE ENFORCE.  Land the verdict,
publish the observed distribution on real ledgers, and only then let a window
suppress anything.  A wrong "committed" suppresses good evidence; a wrong "open"
costs nothing beyond today's status quo.

NOT IN SCOPE, and stated so it is not mistaken for done:

* On REAL data ``_decision_open_index`` computes ``max(b for b in opens if b <=
  delivery_index)``, so ``opened <= delivered`` holds BY CONSTRUCTION and the
  EARLY branch is structurally unreachable from the extractor.  Measuring EARLY
  end-to-end needs the extractor to ask a different question — does ANY boundary
  of ``earliest_event`` exist at-or-before delivery.  This file pins the KERNEL's
  ability to express the verdict; the extractor is a separate change.
* Every GT window is currently ZERO-WIDTH by declaration — ``fact_registry``
  states that for every §1 fact ``earliest_event`` and ``deliver_by`` are the SAME
  boundary.  So EARLY and IN_WINDOW are indistinguishable by declaration until a
  real width is declared.  That is the deeper fix; this is the vocabulary it needs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.runtime import chronological_adjudication as ca  # noqa: E402


def _chron(**kw):
    """A Chronology with every index supplied; overridden per test."""
    base = dict(
        decision_open_index=5,
        delivery_index=7,
        decision_commit_index=9,
        native_acquisition_index=None,
        acknowledgment_index=None,
        action_index=None,
    )
    base.update(kw)
    return ca.Chronology(**base)


#: A non-reactive class whose required_event we can satisfy exactly, so the
#: wrong-event branch never fires and we are testing the ORDER logic alone.
_TYPE = "caller_contract"


def _wanted() -> str:
    from groundtruth.runtime.fact_registry import required_event
    ev = required_event(_TYPE)
    assert ev, "fixture precondition: caller_contract must declare a required_event"
    return ev


def test_delivery_before_its_window_opens_is_EARLY_not_UNMEASURED():
    """THE RED. Landing before the decision opened is a MEASURED timing failure.

    Returning UNMEASURED here is the same lie as reporting a replay error as
    'diffs=0': it renders a precisely-known fact as an absence of knowledge.
    """
    verdict, _wnt = ca._timing(_TYPE, _wanted(), _chron(delivery_index=3))
    assert verdict == ca.EARLY, (
        f"delivered=3 before opened=5 reported {verdict!r}; EARLY is knowable"
    )


def test_late_but_still_correctable_is_LATE_CORRECTIVE():
    """A commit AFTER the late delivery means the agent still had a decision to change."""
    verdict, _w = ca._timing(
        _TYPE, _wanted(),
        _chron(delivery_index=11, decision_commit_index=9, action_index=13),
    )
    assert verdict == ca.LATE_CORRECTIVE, verdict


def test_late_with_nothing_left_to_change_is_EXPIRED():
    """No post-delivery action: the decision was already final. Worthless, not corrective."""
    verdict, _w = ca._timing(
        _TYPE, _wanted(),
        _chron(delivery_index=11, decision_commit_index=9, action_index=None),
    )
    assert verdict == ca.EXPIRED, verdict


def test_in_window_is_unchanged_and_still_named_ON_TIME():
    """NEAR-NEGATIVE. The existing verdict must keep its exact string.

    ON_TIME is consumed by graders and by ss_gate's feature contract; renaming it
    would be a reader/writer break dressed up as a vocabulary improvement.
    """
    verdict, _w = ca._timing(_TYPE, _wanted(), _chron())
    assert verdict == ca.ON_TIME == "ON_TIME"


@pytest.mark.parametrize(
    "kw,expected",
    [
        (dict(native_acquisition_index=6), "STEP_BEHIND"),
        (dict(action_index=7), "UNMEASURED"),   # action == delivery: inverted clock
        (dict(decision_open_index=None), "UNMEASURED"),
    ],
    ids=["step-behind-preserved", "inverted-clock-still-fails-closed", "missing-index"],
)
def test_every_pre_existing_verdict_is_preserved(kw, expected):
    """ANTI-REGRESSION. Widening the vocabulary must not repartition the old cells."""
    verdict, _w = ca._timing(_TYPE, _wanted(), _chron(**kw))
    assert verdict == expected, f"{kw} -> {verdict}, expected {expected}"


def test_the_new_verdicts_are_exported():
    """A verdict a consumer cannot import is a verdict that will be stringly-typed."""
    for name in ("EARLY", "LATE_CORRECTIVE", "EXPIRED"):
        assert name in ca.__all__, f"{name} missing from __all__"
        assert getattr(ca, name) == name


def test_LATE_is_retained_for_historical_artifacts():
    """Old artifacts carry the flat string; readers must still be able to parse it."""
    assert ca.LATE == "LATE"
    assert "LATE" in ca.__all__
