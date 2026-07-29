"""The seven `verify.horizon.executed` terminal reasons must classify as SEVEN things.

WHAT WAS WRONG (verified against `git show HEAD:scripts/swebench/gt_feature_verdicts.py`
on 2026-07-28, run over one-row synthetic ledgers):

    reason                       classify_reason (HEAD)   published verdict (HEAD)
    plan_none_produced           no_evidence              TRIGGER-ABSENT
    no_covering_test_selected    other                    TRIGGER-ABSENT
    covering_no_covering         other                    TRIGGER-ABSENT
    covering_pass                other                    TRIGGER-ABSENT
    covering_unattributable      other                    TRIGGER-ABSENT
    covering_none_produced       other                    TRIGGER-ABSENT
    suppressed_ack_failure       defect                   DELIVERY-FAILURE

Two of those are false statements about the trajectory.

1. `suppressed_ack_failure` -> defect -> DELIVERY-FAILURE.  The seam emits that string at
   the exact call site where `_ss_ack_failure_suppresses` returned True
   (gt_mini_patch.py:11789 / :12062 / :12242): the block WAS built and SS-ACK dropped it as a
   duplicate of an already-acknowledged failure identity.  That referee writes its OWN ledger
   row -- outcome `suppressed_duplicate`, reason `acknowledged_failure_identity` -- which this
   same reader classes `arbitration`.  So ONE event was graded both "a referee doing its job"
   and "the ONLY REAL FAILURE", depending on which of its two rows you read.

2. `covering_none_produced` -> other -> TRIGGER-ABSENT.  That branch
   (gt_mini_patch.py:11767-11775) is reached with an ATTRIBUTED RED in hand that the native
   renderer could not surface.  Evidence existed, bytes never reached the model -- this tool's
   own docstring definition of DELIVERY-FAILURE, and the exact shape of `render_failed`, which
   `_DEFECT_PREFIXES` already grades as a defect.  Reading it as correct-quiet hides a
   renderer bug behind a feature the grader was ALREADY told to expect dark.

Plus the documentation defect these tests exist to stop recurring: the `:113` comment claimed
`plan_none_produced` meant "no covering test exists to execute".  It cannot mean that on the
independent producer path -- `_executed_covering_candidate` returns at :11909 when the covering
selection is EMPTY, so :11924 (the only call into `_verification_plan_emission` from that path)
is reached ONLY with a NON-empty selection.  The genuine capability gap has its own string,
`no_covering_test_selected`, and these tests pin the two apart.
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


def _executed_row(reason: str) -> dict:
    """A verbatim-shaped `verify.horizon.executed` non-delivery row.

    Every one of the seven strings is emitted with kind `verify.horizon.executed`,
    outcome SUPPRESSED_HIDDEN_ONLY and chars=0, so the OUTCOME cannot separate them --
    only the reason can.  That is the whole reason this reader is reason-first.
    """
    return {
        "layer": "verify.horizon.executed",
        "event_type": "post_edit",
        "outcome": "suppressed_hidden_only",
        "reason": reason,
        "chars_delivered": 0,
    }


# ------------------------------------------------------------------ classification ---

@pytest.mark.parametrize(
    "reason,expected_class",
    [
        # correct-quiet: the producer ran and had nothing legitimately deliverable
        ("plan_none_produced", "no_evidence"),
        ("no_covering_test_selected", "no_evidence"),
        ("covering_no_covering", "no_evidence"),
        ("covering_pass", "no_evidence"),
        ("covering_unattributable", "no_evidence"),
        # RED-FIRST at HEAD: was "other"
        ("covering_none_produced", "defect"),
        # RED-FIRST at HEAD: was "defect"
        ("suppressed_ack_failure", "arbitration"),
    ],
)
def test_each_reason_classifies_correctly(reason: str, expected_class: str) -> None:
    klass, detail = gfv.classify_reason(_executed_row(reason))
    assert klass == expected_class, (
        f"{reason!r} classified {klass!r}, expected {expected_class!r}"
    )
    assert detail == reason, "the reason string must survive into the detail verbatim"


def test_no_reason_falls_through_to_other() -> None:
    """`other` is the reader admitting it does not know.  None of the seven may land there:
    an unclassified reason silently inherits the TRIGGER-ABSENT else-branch, which asserts
    'the trigger never occurred' about states where it demonstrably did."""
    seven = (
        "plan_none_produced", "no_covering_test_selected", "covering_no_covering",
        "covering_pass", "covering_unattributable", "covering_none_produced",
        "suppressed_ack_failure",
    )
    unclassified = [r for r in seven if gfv.classify_reason(_executed_row(r))[0] == "other"]
    assert not unclassified, f"still unclassified: {unclassified}"


def test_ack_suppression_agrees_with_the_referees_own_row() -> None:
    """The producer-side string and the referee's own row describe the SAME suppression.
    If they classify differently, the reader contradicts itself on one event."""
    producer = gfv.classify_reason(_executed_row("suppressed_ack_failure"))[0]
    referee = gfv.classify_reason({
        "layer": "verify.horizon.executed",
        "outcome": "suppressed_duplicate",
        "reason": "acknowledged_failure_identity",
        "chars_delivered": 0,
    })[0]
    assert producer == referee == "arbitration", (
        f"same event, two classes: producer-side={producer!r} referee-side={referee!r}"
    )


def test_arbitration_reason_wins_even_if_the_defect_prefix_returns() -> None:
    """Precedence guard.  `suppressed_ack_failure` used to sit in `_DEFECT_PREFIXES`; the
    reason check must be evaluated FIRST so re-adding the prefix cannot silently restore
    the wrong verdict."""
    gfv._DEFECT_PREFIXES = gfv._DEFECT_PREFIXES + ("suppressed_ack_failure",)
    try:
        assert gfv.classify_reason(
            _executed_row("suppressed_ack_failure"))[0] == "arbitration"
    finally:
        gfv._DEFECT_PREFIXES = tuple(
            p for p in gfv._DEFECT_PREFIXES if p != "suppressed_ack_failure")


# ------------------------------------------------------------ published distinguishability ---

def _row(**classes) -> "gfv.FeatureRow":
    row = gfv.FeatureRow(
        feature_id="covering_red",
        kind="FACT",
        bound_fact="covering_red",
        contracted_boundary="test_result",
        registry_surface="post_edit",
        mistake_gated=True,
        gate_note="needs an observed failure with a covering repository test",
    )
    row.reason_classes = Counter({k: v for k, v in classes.items()})
    return row


def _decide_from_reason(reason: str) -> "gfv.FeatureRow":
    klass, detail = gfv.classify_reason(_executed_row(reason))
    row = _row(**{klass: 1})
    row.reason_detail = Counter({f"{klass}:{detail}": 1})
    gfv._decide(row, Counter(), 0)
    return row


def test_capability_gap_is_distinguishable_from_a_green_test() -> None:
    """THE headline requirement.  Both publish TRIGGER-ABSENT, and they mean opposite
    things: `no_covering_test_selected` = GT's graph knows NO covering test for the edited
    symbols (a capability gap GT owns); `covering_pass` = a covering test ran and was green
    (nothing to report).  One number for both is how the gap stays invisible."""
    gap = _decide_from_reason("no_covering_test_selected")
    green = _decide_from_reason("covering_pass")
    assert gap.verdict == green.verdict == "TRIGGER-ABSENT"
    assert gfv.verdict_cell(gap) != gfv.verdict_cell(green), (
        "the two states publish an identical cell: the capability gap is still hidden"
    )
    assert gfv.verdict_cell(gap) == "TRIGGER-ABSENT(no_covering_test_selected)"
    assert gfv.verdict_cell(green) == "TRIGGER-ABSENT(covering_pass)"


def test_all_five_trigger_absent_states_publish_five_distinct_cells() -> None:
    cells = {
        r: gfv.verdict_cell(_decide_from_reason(r))
        for r in ("plan_none_produced", "no_covering_test_selected",
                  "covering_no_covering", "covering_pass", "covering_unattributable")
    }
    assert len(set(cells.values())) == 5, f"cells collapse: {cells}"
    assert {c.split("(", 1)[0] for c in cells.values()} == {"TRIGGER-ABSENT"}, (
        "sub-detail must NOT become a new top-level verdict"
    )


def test_ack_suppression_publishes_arbitrated_not_delivery_failure() -> None:
    row = _decide_from_reason("suppressed_ack_failure")
    assert row.verdict == "ARBITRATED", (
        "a referee dedup still reads as the tool's ONLY REAL FAILURE"
    )
    assert row.verdict_detail == "suppressed_ack_failure"


def test_renderer_gap_publishes_delivery_failure() -> None:
    row = _decide_from_reason("covering_none_produced")
    assert row.verdict == "DELIVERY-FAILURE", (
        "an attributed RED the renderer could not surface is evidence produced whose bytes "
        "never reached the model -- reading it as correct-quiet hides a renderer bug"
    )
    assert row.verdict_detail == "covering_none_produced"


# ------------------------------------------------------------------- anti-weakening ---

def test_positive_control_delivery_still_reads_FIRED() -> None:
    """If FIRED stopped working, every assertion above would pass vacuously."""
    row = _row(no_evidence=1)
    row.delivered = 2
    gfv._decide(row, Counter(), 0)
    assert row.verdict == "FIRED"
    assert gfv.verdict_cell(row) == "FIRED", "FIRED must not grow a redundant (delivered)"


def test_the_verdict_vocabulary_is_unchanged() -> None:
    """No new TOP-LEVEL verdict was invented.  The sub-detail rides inside the existing
    five so every counter, and the split pinned by
    tests/swebench/test_verdict_funnel_splits_trigger_absent_20260727.py, keep their
    meaning."""
    assert {gfv._VERDICT_FIRED, gfv._VERDICT_ABSENT, gfv._VERDICT_FAILURE,
            gfv._VERDICT_ARBITRATED, gfv._VERDICT_UNINSTRUMENTED} == {
        "FIRED", "TRIGGER-ABSENT", "DELIVERY-FAILURE", "ARBITRATED", "NO-INSTRUMENTATION"}


def test_unknown_reasons_still_reach_other() -> None:
    """ANTI-OVERFIT.  The fix must classify the strings the seam ACTUALLY emits, not blanket
    every `covering_*` prefix.  `covering_unavailable` (an execution that could not run) is
    deliberately still unclassified, and the reader must keep SAYING so rather than guessing."""
    assert gfv.classify_reason(_executed_row("covering_unavailable"))[0] == "other"
    assert gfv.classify_reason(_executed_row("covering_empty_sub_fact_floor"))[0] == "other"
