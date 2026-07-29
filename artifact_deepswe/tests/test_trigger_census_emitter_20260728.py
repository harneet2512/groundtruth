"""The trigger census: a measured denominator for every "this feature was dark" claim.

WHY.  A producer that was never ASKED and a producer that is BROKEN are the same silence
today, so "eleven of the seventeen features are dark" is not a gradeable statement.  This
records that a trigger's boundary OCCURRED — that the producer had an opportunity — which
turns a later non-delivery from an unfalsifiable absence into a measured negative.

THREE DESIGN DECISIONS PINNED HERE, each one a mistake avoided:

1. **Rows fire only for triggers whose boundary actually occurred.**  Emitting a row for
   every registered trigger on every observation would count correct-quiet as a missed
   opportunity — manufacturing the exact phantom darkness this exists to remove.
2. **The authority is ``required_event``, not ``deliver_by``.**  Eight registered
   evidence types disagree, including two of the 17 DIRECT.  Each row carries
   ``declared_deliver_by`` and ``deliver_by_overridden`` so a future reader that consults
   the wrong field is falsifiable from the artifact itself.
3. **Emitted where the semantic events are COMPLETE.**  The seam's own ``_semantic_arg``
   carries only three boundaries; ``_observe_semantic_events`` adds search_result /
   failed_search / failure_obs unconditionally and completes on first consumption.
   Reading the seam's list would have silently undercounted six of the nine.

ZERO MODEL BYTES.  Host-side ledger only, ``chars_delivered=0``, and ``outcome`` is
``"evaluated"`` — never ``"delivered"`` — so no delivery view can see these rows.  Default
OFF behind ``_inseam_metrics_on()``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    build_observation_binding,
)


@pytest.fixture()
def census(monkeypatch):
    """Capture rows with the census ON and an observation identity published."""
    rows: list[dict] = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda e: rows.append(e) or True)
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_emitted_trigger_ids", set())
    monkeypatch.setattr(g, "_action_count", 4)
    g._delivery_observation_context.set(build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64, parent_policy_chars=0,
        action_batch_sha256="b" * 64, candidate_ordinal=0,
        candidate_kind="k", candidate_id="c"))
    yield rows
    g._delivery_observation_context.set(None)


def test_a_boundary_that_occurred_yields_its_triggers(census):
    """DECISION 1, positive half."""
    g._record_trigger_opportunities(("edit_result",))

    assert census, "an edit_result observation is an opportunity for several facts"
    types = {r["evidence_type"] for r in census}
    assert {"syntax_result", "signature_mismatch", "caller_break"} <= types
    assert all(r["required_event"] == "edit_result" for r in census)


def test_a_boundary_that_did_NOT_occur_yields_nothing(census):
    """DECISION 1, the half that matters more.

    ``submit_refusal`` fires at ``submit``. On an edit observation it is correctly quiet,
    and recording an opportunity for it would invent a missed one.
    """
    g._record_trigger_opportunities(("edit_result",))
    assert "submit_refusal" not in {r["evidence_type"] for r in census}


def test_the_overrides_are_carried_so_a_wrong_reader_is_falsifiable(census):
    """DECISION 2. caller_break declares deliver_by=file_view but fires at edit_result."""
    g._record_trigger_opportunities(("edit_result",))

    row = next(r for r in census if r["evidence_type"] == "caller_break")
    assert row["required_event"] == "edit_result"
    assert row["declared_deliver_by"] == "file_view"
    assert row["deliver_by_overridden"] is True


def test_rows_can_never_be_mistaken_for_a_delivery(census):
    """ZERO BYTES. `outcome != "delivered"` and chars=0 keep these out of every
    delivery view, and out of the delivered-row seal join."""
    g._record_trigger_opportunities(("edit_result", "submit"))

    for row in census:
        assert row["outcome"] == "evaluated" != "delivered"
        assert row["chars_delivered"] == 0
        assert "content_sha256_16" not in row


def test_default_off_emits_nothing(monkeypatch):
    """The flag is a real gate, not decoration."""
    rows: list[dict] = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda e: rows.append(e) or True)
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: False)

    g._record_trigger_opportunities(("edit_result",))

    assert rows == []


def test_no_observation_identity_means_no_row(census, monkeypatch):
    """A row nothing can join is worse than no row: it inflates the denominator while
    being unattributable to any producer outcome."""
    g._delivery_observation_context.set(None)
    g._record_trigger_opportunities(("edit_result",))
    assert census == []


def test_one_observation_counts_each_trigger_once(census):
    """Several actions can share a policy observation; the denominator must not inflate."""
    g._record_trigger_opportunities(("edit_result",))
    first = len(census)
    g._record_trigger_opportunities(("edit_result",))

    assert len(census) == first
    ids = [r["trigger_opportunity_id"] for r in census]
    assert len(ids) == len(set(ids))


def test_a_new_observation_re_arms_the_census(monkeypatch):
    """ANTI-REGRESSION on the dedup set. If it were not cleared per observation,
    observation 2 would silently emit nothing and read as dark."""
    rows: list[dict] = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda e: rows.append(e) or True)
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    binding = build_observation_binding(
        batch_start_iteration=0, parent_policy_sha256="a" * 64, parent_policy_chars=0,
        action_batch_sha256="b" * 64, candidate_ordinal=0,
        candidate_kind="k", candidate_id="c")

    g._begin_legacy_observation({"command": "sed -i s/a/b/ x.py"})
    g._delivery_observation_context.set(binding)
    g._record_trigger_opportunities(("edit_result",))
    first = len(rows)
    assert first

    g._begin_legacy_observation({"command": "sed -i s/c/d/ y.py"})
    g._delivery_observation_context.set(binding)
    g._record_trigger_opportunities(("edit_result",))

    assert len(rows) == first * 2, "the second observation emitted no census"
    g._delivery_observation_context.set(None)


def test_instrumentation_fault_cannot_break_the_loop(census, monkeypatch):
    monkeypatch.setattr(
        g, "_ledger_line_direct",
        lambda e: (_ for _ in ()).throw(RuntimeError("ledger fault")))

    g._record_trigger_opportunities(("edit_result",))   # must not raise
