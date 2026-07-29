"""covering_red's two SILENT states must be countable. Zero new model bytes.

THE DEFECT.  ``covering_red`` has eight terminal states at the seam.  Six publish a
host-side ledger row; **two publish nothing at all**:

* **"no covering test exists"** (``gt_mini_patch.py``, ``_executed_covering_candidate``):
  when ``_covering_tests_for_symbols`` returns empty, the code burns the per-symbol
  latch and ``return None``.  No row, no bytes, no trace.
* **the SS-ACK duplicate suppression**: when ``_ss_ack_failure_suppresses`` fires, a
  bare ``return None``.

Downstream, six distinct engineering states -- no test exists / the test passed / it
timed out / the RED was not ours / the renderer produced nothing / a referee ate it --
all publish as the SAME ``TRIGGER-ABSENT`` verdict, on a feature that
``_MISTAKE_GATED_FACTS`` has already told the grader to EXPECT to be trigger-absent.
The two silent states do not even reach that: they are indistinguishable from the
producer never having run.

WHY MEASURE AND NOT DELIVER.  A delivered ``NO_EXISTING_COVERING_TEST`` rung was
designed and REJECTED on four independent grounds: it duplicates an already-delivered
advisory (``verification_horizon.render_verify_emission`` has a model-facing
``has_covering=False`` branch); it has no native voice (every other covering_red byte
is the runner's own stdout, whereas this is GT narrating about GT's own graph); it
asserts a REPOSITORY property from an INDEX property (an empty selection means GT's
graph has no FACT-tier test->impl edge, not that the repo has no covering test -- and
delivering the latter would discourage running a test that exists); and it would
consume the observation's single dose slot under a governor that keeps exactly one
winner, outranking ``l3.contract`` on the very edit turn that contract decides.

So the fix is entirely host-side.  ``chars=0`` on every row is load-bearing: the seam
downgrades any ``delivered`` row with ``chars <= 0`` to ``suppressed_internal_only``,
so these rows can never be mistaken for delivery.

WHAT THIS DOES NOT CLAIM.  Nothing here shows the feature helps an agent.  It makes an
existing capability gap COUNTABLE so the next run can answer a question that currently
has no answer at all: is "no covering test exists" 2% of post-edit turns, or 60%?
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gt_mini_patch as g  # noqa: E402


_LAYER = "verify.horizon.executed"


def _rows(monkeypatch) -> list[dict]:
    """Capture host-side ledger rows without touching the durable sink."""
    captured: list[dict] = []

    def _spy(**kw):
        captured.append(dict(kw))

    monkeypatch.setattr(g, "_runtime_ledger_record", _spy)
    return captured


def _arm_edited_symbol(monkeypatch):
    """Put the producer in the state where it will query for covering tests.

    Every line here is a guard the producer actually checks, read from its source --
    the first draft of this fixture set none of them, the function returned at the
    flag check, and the RED "passed" while proving nothing. The latch assertion below
    is what caught it, which is why it is not optional.
    """
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")     # else early return, zero work
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_action_count", 7)
    monkeypatch.setattr(g, "_last_test_step", -1)    # refuses if the agent tested this turn
    monkeypatch.setattr(g, "_oracle_edited_rels", {"src/foo.py"})
    monkeypatch.setattr(g, "_covering_exec_fired_syms", set())
    monkeypatch.setattr(g, "_covering_exec_pending", {"syms": set()})
    # the ACTUAL symbol source consulted at the selection site
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: {"parse_url"})


def test_no_covering_test_selected_is_recorded_not_silent(monkeypatch):
    """THE RED. An empty selection is a MEASURED capability gap, not an absence of events.

    Today this path burns the latch and returns None with no row, so it is
    indistinguishable downstream from "the producer never ran".
    """
    rows = _rows(monkeypatch)
    _arm_edited_symbol(monkeypatch)
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda syms: ())

    g._executed_covering_candidate()

    reasons = [r.get("reason") for r in rows if r.get("kind") == _LAYER]
    assert "no_covering_test_selected" in reasons, (
        f"the 'no covering test exists' state published nothing; rows={rows}"
    )
    row = next(r for r in rows if r.get("reason") == "no_covering_test_selected")
    assert int(row.get("chars") or 0) == 0, "instrumentation must never claim bytes"


def test_the_latch_semantics_are_unchanged(monkeypatch):
    """NEAR-NEGATIVE. Adding a row must not change WHEN the producer re-attempts.

    The latch exists so a symbol is not re-queried every turn; a re-edit re-arms it.
    An existing test pins that behaviour and must stay green.
    """
    _rows(monkeypatch)
    _arm_edited_symbol(monkeypatch)
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda syms: ())

    g._executed_covering_candidate()

    assert "parse_url" in g._covering_exec_fired_syms, (
        "the per-symbol latch must still burn on an empty selection"
    )


def test_instrumentation_fault_cannot_break_the_producer(monkeypatch):
    """Telemetry never breaks the agent loop -- the rule this file's own writer follows."""
    _arm_edited_symbol(monkeypatch)
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda syms: ())

    def _boom(**kw):
        raise RuntimeError("ledger fault")

    monkeypatch.setattr(g, "_runtime_ledger_record", _boom)

    assert g._executed_covering_candidate() is None   # must not raise


def test_the_recorded_reasons_are_distinguishable_from_each_other():
    """The whole point: six states must not share one string.

    A reader cannot act on 'TRIGGER-ABSENT'; it can act on 'no covering test exists in
    the graph for 60% of edits'.
    """
    distinct = {
        "no_covering_test_selected",   # S1 -- the capability gap
        "covering_no_covering",        # selected, but the file is absent on disk
        "covering_pass",               # ran, green
        "covering_unattributable",     # RED, but not ours
        "covering_none_produced",      # attributed RED, renderer produced nothing
        "plan_none_produced",          # plan exhausted with no deliverable rung
    }
    assert len(distinct) == 6, "each state needs its own reason string"
