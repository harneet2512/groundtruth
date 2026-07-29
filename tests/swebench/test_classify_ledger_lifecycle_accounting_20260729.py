"""Lifecycle accounting: `produced`/`delivered` must count FACTS, not markers.

THE DEFECT (codex audit 2026-07-29, confirmed at the emission sites):
`classify_ledger` incremented `produced` for EVERY layer-mapped row and `delivered`
for every `outcome=="delivered"` row. But the seam writes rows that are markers,
not facts, and its own docstrings state the discriminators this reader ignored:

  * ss_ack annotation rows ride ``outcome="delivered"`` with ``chars_delivered=0``,
    ``event_type="ack"``, ``reason="ss_ack"`` — and the writer's contract says
    "every delivered-payload / dose / leak view (which requires chars_delivered>0)
    excludes it" (gt_mini_patch.py `_ss_emit_ack_row`, L20134-20138). This reader
    required no positive chars, so an acknowledgment masqueraded as a delivery
    AND as production.
  * ``outcome="eligible" reason="producer_boundary" chars=0`` (`_inseam_eligible`,
    L429-437) marks that a producer COULD fire — an opportunity, never production.
  * ``outcome="evaluated"`` census rows (`_record_trigger_opportunities`) are the
    denominator for dark, never production.
  * ``allow``/``submit_clean``/``clean``/``allow_clean`` mean the gate RAN and
    correctly produced NO fact — counted as ``allowed`` (correct silence), not
    ``produced``.

Consumer coupling pinned here too: `submit_refusal`'s correct_abstain arm read
``produced > 0 and delivered == 0 and allowed > 0`` — with allow rows no longer
manufacturing `produced`, the arm must key on ``allowed`` alone, or a clean-gate
task silently loses its correct_abstain verdict.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO), str(_REPO / "scripts" / "swebench"), str(_REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from gt_feature_metrics import classify_ledger, layer_to_fact_class  # noqa: E402


def _row(**kw) -> dict:
    base = {
        "layer": "submit_gate", "event_type": "", "file_path": "",
        "outcome": "", "reason": "", "chars_delivered": 0, "iteration": 1,
    }
    base.update(kw)
    return base


def test_layer_fixture_is_mapped() -> None:
    """Guard the fixture premise: these layers resolve to fact classes at all."""
    assert layer_to_fact_class("submit_gate") == "submit_refusal"
    assert layer_to_fact_class("l3.contract") == "caller_contract"


def test_ack_row_is_neither_delivered_nor_produced() -> None:
    """An ss_ack annotation (outcome=delivered, chars=0, event=ack) is metadata about
    an earlier delivery — the writer's own contract excludes it from every
    delivered-payload view. It must not count as delivered OR produced."""
    per = classify_ledger([
        _row(layer="l3.contract", outcome="delivered", event_type="ack",
             reason="ss_ack", ack=True, content_sha256_16="ab" * 8),
    ])
    b = per["caller_contract"]
    assert b["delivered"] == 0, "ack row masqueraded as a delivery"
    assert b["produced"] == 0, "ack row manufactured production"


def test_zero_char_delivered_row_is_internal_telemetry() -> None:
    """Any outcome=delivered row with chars_delivered<=0 is internal telemetry by the
    seam's contract — never a payload delivery."""
    per = classify_ledger([
        _row(layer="l3.contract", outcome="delivered", chars_delivered=0),
    ])
    assert per["caller_contract"]["delivered"] == 0


def test_real_delivery_still_counts() -> None:
    per = classify_ledger([
        _row(layer="l3.contract", outcome="delivered", chars_delivered=244,
             event_type="edit_result", file_path="a/x.py"),
    ])
    b = per["caller_contract"]
    assert b["delivered"] == 1
    assert b["produced"] == 1
    assert b["delivered_chars"] == 244
    assert "a/x.py" in b["delivered_files"]


def test_eligible_marker_is_not_production() -> None:
    """`_inseam_eligible` writes outcome=eligible chars=0 at the producer boundary —
    an opportunity marker. It must not increment produced."""
    per = classify_ledger([
        _row(outcome="eligible", reason="producer_boundary"),
    ])
    assert per["submit_refusal"]["produced"] == 0


def test_evaluated_census_is_not_production() -> None:
    per = classify_ledger([
        _row(outcome="evaluated", reason="trigger_present"),
    ])
    assert per["submit_refusal"]["produced"] == 0


def test_allow_counts_as_allowed_not_produced() -> None:
    """A clean gate verdict is correct silence: tracked in `allowed`, never `produced`."""
    per = classify_ledger([
        _row(outcome="allow", reason="submit_clean"),
    ])
    b = per["submit_refusal"]
    assert b["allowed"] == 1
    assert b["produced"] == 0


def test_suppressed_candidate_is_production() -> None:
    """A suppressed arbiter loser WAS produced — suppression is post-production."""
    per = classify_ledger([
        _row(layer="l3.contract", outcome="suppressed_hidden_only",
             reason="global_arbiter:outranked"),
    ])
    assert per["caller_contract"]["produced"] == 1


def test_correct_abstain_survives_on_allow_alone() -> None:
    """Consumer coupling: with allow rows excluded from `produced`, the
    submit_refusal correct_abstain verdict must still be MEASURED True for a
    task whose only gate row is a clean allow."""
    # The verdict function needs the full grading context; pin the arm's inputs
    # instead: the classify_ledger output that feeds it.
    per = classify_ledger([_row(outcome="allow", reason="submit_clean")])
    b = per["submit_refusal"]
    # the arm's new predicate: delivered == 0 and allowed > 0  → correct_abstain
    assert b["delivered"] == 0 and b["allowed"] > 0
