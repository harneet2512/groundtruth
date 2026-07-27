"""TRIGGER-ABSENT conflates five different things, and only two of them are correct quiet.

THE PROBLEM, stated by the code itself. `_decide` in `scripts/swebench/gt_feature_verdicts.py`
carries this comment on the arbitration branch:

    "the three-verdict taxonomy has no fourth bucket, so it is counted as TRIGGER-ABSENT"

So a feature whose evidence WAS produced and was then withheld by a referee reports the same
headline verdict as a feature whose trigger never occurred. It also reports the same verdict as a
feature for which NO LEDGER ROW EXISTS AT ALL -- which is not quiet, it is blindness.

Today TRIGGER-ABSENT means any of:
  1. ARBITRATED        evidence existed; a referee withheld it        -> NOT quiet, a suppression
  2. no_evidence       trigger_false / plan_none_produced / clean     -> correct quiet
  3. telemetry         only bookkeeping rows                          -> unproven
  4. other             unclassified reason strings                    -> unproven
  5. no rows at all    the producer layer never emitted anything      -> BLIND, not quiet

Reporting these as one number is how "12 of 14 mistake-gated = correct-quiet" became a comfortable
headline that hid suppressions and missing instrumentation. The tool ALREADY computes the
distinction -- `classify_reason` returns eight classes -- it simply refuses to publish it.

WHAT THIS FILE REQUIRES. Distinct verdicts for the distinct states, so the headline cannot launder
a suppression or a blind spot into correct quiet. The classifier is unchanged; only the published
verdict is split. Nothing here weakens a bar: FIRED and DELIVERY-FAILURE keep their exact meaning.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gt_feature_verdicts",
    Path(__file__).resolve().parents[2] / "scripts" / "swebench" / "gt_feature_verdicts.py",
)
assert _SPEC and _SPEC.loader
gfv = importlib.util.module_from_spec(_SPEC)
sys.modules["gt_feature_verdicts"] = gfv
_SPEC.loader.exec_module(gfv)


def _row(**classes) -> "gfv.FeatureRow":
    row = gfv.FeatureRow(
        feature_id="probe_fact",
        kind="FACT",
        bound_fact="probe_fact",
        contracted_boundary="edit_result",
        registry_surface="post_edit",
        mistake_gated=False,
        gate_note="",
    )
    row.reason_classes = Counter(classes)
    return row


def _decide(row) -> str:
    gfv._decide(row, Counter(), 0)
    return row.verdict


def test_positive_control_delivered_still_reads_FIRED():
    """The instrument must still produce its other values, or the assertions below prove
    nothing about the split."""
    row = _row(produced=1)
    row.delivered = 3
    assert _decide(row) == "FIRED"


def test_positive_control_wrong_phase_still_reads_DELIVERY_FAILURE():
    assert _decide(_row(wrong_phase=2)) == "DELIVERY-FAILURE"


def test_arbitrated_is_NOT_reported_as_trigger_absent():
    """A referee withholding produced evidence is a SUPPRESSION. Calling it trigger-absent
    tells the reader the trigger never happened, which is false -- the evidence existed."""
    verdict = _decide(_row(arbitration=4))
    assert verdict != "TRIGGER-ABSENT", (
        "arbitrated evidence is still laundered into TRIGGER-ABSENT; a suppression reads as "
        "correct quiet"
    )


def test_no_rows_at_all_is_NOT_reported_as_trigger_absent():
    """No ledger row for the producer layer is BLINDNESS, not quiet. The trigger may well have
    occurred; nothing was recorded either way, so the honest verdict is not-evaluable."""
    verdict = _decide(_row())          # no classes at all -> "no rows"
    assert verdict != "TRIGGER-ABSENT", (
        "a feature with NO instrumentation reads as correct quiet -- absence of evidence is "
        "being reported as evidence of absence"
    )


def test_genuine_no_evidence_still_reads_trigger_absent():
    """ANTI-WEAKENING. The split must not erase the legitimate case: trigger_false /
    plan_none_produced / clean genuinely mean the opportunity did not arise."""
    assert _decide(_row(no_evidence=5)) == "TRIGGER-ABSENT"


def test_the_four_states_are_mutually_distinguishable():
    """The whole point: four inputs, four different published verdicts. If any two collapse,
    the headline can still hide one behind the other."""
    verdicts = {
        "arbitrated": _decide(_row(arbitration=1)),
        "no_rows": _decide(_row()),
        "no_evidence": _decide(_row(no_evidence=1)),
        "wrong_phase": _decide(_row(wrong_phase=1)),
    }
    assert len(set(verdicts.values())) == 4, f"verdicts collapse: {verdicts}"
