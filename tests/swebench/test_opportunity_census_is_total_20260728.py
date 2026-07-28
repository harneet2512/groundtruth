r"""The opportunity census must be TOTAL: every claimed opportunity leaves exactly one record.

THE DEFECT. `join_opportunity_deliveries` had TWO silent drops:

    if issues:
        continue                                  # <- validation failure: vanished
    try:
        binding = observation_binding_from_dict(binding_dict)
    except (TypeError, ValueError):
        continue                                  # <- unconstructible binding: vanished

A dropped row appeared in NEITHER `per_opportunity` NOR the feature roll-up. Downstream, a row that
vanished is indistinguishable from a row that never existed -- so an analysis could report
"N opportunities, all accounted for" while N was quietly short. That is the exact "silent zero"
this module's own header warns about, reproduced inside the module.

Worse, the two halves DISAGREED about the same corpus: `collect_feature_opportunities` counted
malformed rows (`malformed_rows`) and poisoned the affected feature to UNMEASURED, while the join
silently discarded them. Two readers, one ledger, different totals, and nothing to reconcile them.

The second drop was invisible even to that counter, because `collect_feature_opportunities` never
rehydrates the binding -- a row can pass field validation and still fail to construct one.

WHY TOTALITY IS THE PROPERTY, not "handle malformed rows". The design requirement is that every
(feature, observation) pair lands in exactly one disposition cell and the cells SUM to N. Only then
can "we don't know whether this opportunity was taken" stop being a possible answer. A test that
merely checked the new state existed would not pin that; these assert the sum.

NO BAR IS WEAKENED. A malformed row is recorded, never promoted: it is deliberately NOT indexed
into `opportunities_by_id`, so a delivery claiming it still resolves to BROKEN_BINDING.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT, _ROOT / "src", _ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import feature_opportunity as fo  # noqa: E402


def _opportunity_row(**over) -> dict:
    """A row shaped like the seam's `feature.opportunity` emission."""
    row = {
        "layer": "feature.opportunity",
        "opportunity_id": "opp-1",
        "observation_id": "obs-1",
        "candidate_id": "cand-1",
        "iteration": 1,
        "delivery_eligible": True,
        "selected": True,
        "outcome": "eligible",
        "reason": "formatter_visible_candidate",
    }
    row.update(over)
    return row


def _count_opportunity_rows(rows: list[dict]) -> int:
    return sum(
        1 for r in rows
        if isinstance(r, dict) and r.get("layer") == "feature.opportunity"
    )


def _join(rows: list[dict]):
    return fo.join_opportunity_deliveries(
        ledger_rows=rows, messages=[], consumption_ledger={}, inventory={},
    )


def test_positive_control_the_join_runs_and_returns_the_expected_shape():
    """Run FIRST. If the join cannot produce records at all, every count below is unreadable --
    a total of zero would trivially 'equal' a total of zero."""
    result = _join([_opportunity_row()])
    assert isinstance(result, dict), f"join returned {type(result)!r}"
    assert "per_opportunity" in result, sorted(result)
    assert isinstance(result["per_opportunity"], list)
    assert len(result["per_opportunity"]) == 1, (
        "the control row produced no record; the fixture does not exercise the join"
    )


def test_the_new_state_exists_and_is_distinct():
    assert fo.MALFORMED_OPPORTUNITY == "MALFORMED_OPPORTUNITY"
    assert fo.MALFORMED_OPPORTUNITY not in {
        fo.DELIVERED_FIRE, fo.BOUND_NO_OP, fo.MISSED_FIRE, fo.BROKEN_BINDING,
    }


def test_a_malformed_row_is_ACCOUNTED_FOR_not_dropped():
    """THE DEFECT. A row that fails validation must still leave a record naming why."""
    rows = [_opportunity_row(opportunity_id=None)]  # not a str -> validation issue
    per_opp = _join(rows)["per_opportunity"]
    assert len(per_opp) == 1, "the malformed row vanished from the census"
    assert per_opp[0]["state"] == fo.MALFORMED_OPPORTUNITY
    assert per_opp[0]["reason"], "the record does not say WHY it is unusable"


def test_a_duplicate_opportunity_id_is_accounted_for():
    """Duplicate ids were also silently dropped by the same `if issues: continue`."""
    rows = [_opportunity_row(), _opportunity_row()]
    per_opp = _join(rows)["per_opportunity"]
    assert len(per_opp) == _count_opportunity_rows(rows), (
        "the duplicate vanished; the census no longer sums"
    )
    assert any(r["state"] == fo.MALFORMED_OPPORTUNITY for r in per_opp)


def test_TOTALITY_every_claimed_opportunity_leaves_exactly_one_record():
    """THE PROPERTY. Mixed corpus: valid, malformed, duplicate. len(records) == len(rows).

    This is the assertion that makes 'unknown' impossible. If it ever fails, some ledger row is
    being consumed without a verdict, and any downstream census built on this join is short by an
    unknown amount.
    """
    rows = [
        _opportunity_row(opportunity_id="a"),
        _opportunity_row(opportunity_id=None),           # malformed
        _opportunity_row(opportunity_id="a"),            # duplicate of the first
        _opportunity_row(opportunity_id="b", selected=False),
        {"layer": "something.else"},                     # not an opportunity: must be ignored
    ]
    per_opp = _join(rows)["per_opportunity"]
    assert len(per_opp) == _count_opportunity_rows(rows) == 4, (
        f"census is not total: {len(per_opp)} records for "
        f"{_count_opportunity_rows(rows)} opportunity rows"
    )
    assert all(r.get("state") for r in per_opp), "a record carries no state"


def test_a_malformed_row_can_NEVER_originate_a_delivery():
    """NO BAR WEAKENED. Recording a malformed row must not let it become the originating
    opportunity for a fire -- otherwise 'account for everything' would quietly become
    'admit everything', and a delivery with an invalid binding would gain a false provenance.

    HONEST LIMITATION -- THIS ASSERTION IS NOT MUTATION-VERIFIED. A mutation that deliberately
    promotes malformed rows into `opportunities_by_id` was applied (sentinel verified on disk,
    not merely attempted) and killed NOTHING, even after tightening from the state to the reason
    code. With an empty `consumption_ledger` the physical-fire join fails regardless, so this
    fixture cannot distinguish "origin lookup failed" from "origin found, fire did not join".

    Closing it needs a fixture carrying a REAL physical delivery so the post-origin path is
    actually exercised. Until then this documents intent and guards the obvious regression, but
    it must NOT be cited as proof that promotion is impossible. Recorded rather than quietly
    left as an assertion that looks stronger than it is.
    """
    rows = [
        _opportunity_row(opportunity_id=None),
        {
            "layer": "delivery",
            "outcome": "delivered",
            "chars_delivered": 42,
            "observation_binding": {"opportunity_id": "opp-1"},
        },
    ]
    result = _join(rows)
    per_delivery = result["per_delivery"]
    assert per_delivery, "the delivery row produced no record"
    # Assert the REASON, not just the state. `BROKEN_BINDING` is over-determined here -- with an
    # empty consumption ledger the physical-fire join fails anyway, so a state-only assertion
    # passes even if the malformed row WERE promoted to an origin. Caught by mutation: indexing
    # malformed rows into `opportunities_by_id` killed no test until this was tightened. The
    # reason code is the only field that distinguishes "origin lookup failed" (what must happen)
    # from "origin found, fire did not join" (the bar being weakened).
    assert all(
        d["reason"] == "fire_without_exact_originating_opportunity"
        for d in per_delivery
    ), (
        f"a delivery found an originating opportunity despite it being MALFORMED; malformed rows "
        f"must be recorded but never promoted. got: {[d['reason'] for d in per_delivery]}"
    )
    assert all(d["state"] == fo.BROKEN_BINDING for d in per_delivery)


def test_non_opportunity_rows_are_not_counted():
    """Guard against the fix over-reaching into rows that never claimed to be opportunities."""
    per_opp = _join([{"layer": "delivery"}, {"layer": "feature.other"}])["per_opportunity"]
    assert per_opp == []
