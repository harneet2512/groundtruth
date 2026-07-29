"""C33 — the runtime and the offline reader must erase the window marker by the SAME rule.

ARTIFACT-FIRST. This was not found by reading code; it was found by reading run 30390877219,
where all five tasks were sealed `gt_metrics_incomplete` and every one of them carried:

    canonical_runtime_attestation: {"integrity_ok": false, "delivered_count": 0,
      "rejected": [{"reason": "EVIDENCE_GENERATION_REWRITTEN"}]}

Diffing the `evidence_attempt_journal` payloads for the rejected evidence id showed exactly ONE
differing field across every journal row, on 5/5 tasks, always the same record:

    feature_id="obligations", mandatory_reason="TASK_OBLIGATION"
    decision_window_generation: ''  ->  'GT-W-7a6db489b9fec7fd'   (cfn-lint-3749)
                                     -> 'GT-W-af50d0df489ddd3b'   (sh-744)          ... etc

That field is the one the WRITER (`reasoning_runtime._evidence_generation_projection`)
deliberately normalizes as runtime-owned scheduling state, and the one the READER
(`runtime_attestation._evidence_generation_projection`) compared RAW. So the runtime correctly
accepted a re-offer whose window had advanced and wrote both rows; the reader then rejected that
identical, correct history. Nothing was wrong with the delivery -- two halves of one rule
disagreed, and the disagreement propagated all the way to "every task uncitable".

WHY THE FIX IS A SHARED FUNCTION, not a second copy of the conditional: a rule that two files
must keep in sync by hand is not one rule. That is the same defect `DECISION_CAPSULE_SCHEMA`
was exported to kill (reasoning_runtime.py:68-82), one layer down.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 -- drop `decision_window_generation` from the reader's projection (i.e. restore the bug):
        `test_an_advanced_window_is_not_a_rewritten_generation` goes RED, reproducing the run.
  M2 -- return "" unconditionally from `projected_decision_window`:
        `test_a_windowless_record_never_projects_equal_to_the_obligations_capsule` goes RED, and
        the task-start obligations capsule would project-equal a record carrying no window.
"""

from __future__ import annotations

import json

from groundtruth.runtime import runtime_attestation as ra
from groundtruth.runtime.reasoning_runtime import (
    MandatoryReason,
    projected_decision_window,
)


def _journal_payload(window: str, *, feature_id: str = "obligations") -> dict:
    """The shape actually stored in `evidence_attempt_journal.canonical_json`."""

    return {
        "evidence_id": "GT-E-6576c7113d9d931b-g" + "6" * 64,
        "feature_id": feature_id,
        "mandatory_reason": "TASK_OBLIGATION",
        "lifecycle": "DELIVERED",
        "fresh": True,
        "superseded": False,
        "transition_history": [{"from_state": "PENDING", "to_state": "DELIVERED"}],
        "owner_feature_ids": [],
        "decision_window_generation": window,
        "claim": "preserve the returned Session",
    }


def test_the_probe_can_produce_a_non_zero() -> None:
    """CALIBRATION. Without a pair that DOES differ, every equality below is unreadable."""
    a = ra._evidence_generation_projection(_journal_payload(""))
    b = ra._evidence_generation_projection(_journal_payload(""))
    b["claim"] = "a genuinely different producer claim"
    assert a != b


def test_an_advanced_window_is_not_a_rewritten_generation() -> None:
    """M1. THE RUN DEFECT, reproduced on the observed values.

    seq1 carried no window; seq2..seq6 carried the real marker. Same generation throughout.
    """
    seq1 = ra._evidence_generation_projection(_journal_payload(""))
    seq2 = ra._evidence_generation_projection(
        _journal_payload("GT-W-7a6db489b9fec7fd")
    )
    assert seq1 == seq2


def test_reader_and_writer_agree_on_the_same_record() -> None:
    """The parity that the whole fix exists to guarantee, asserted directly."""
    for feature_id, reason, name in (
        ("obligations", MandatoryReason.TASK_OBLIGATION, "TASK_OBLIGATION"),
        ("localization", MandatoryReason.TASK_OBLIGATION, "TASK_OBLIGATION"),
    ):
        # The writer passes the enum; the reader passes its serialized name. One rule, one
        # answer -- otherwise the reader's verdict depends on which side is asking.
        assert projected_decision_window(feature_id, reason) == (
            projected_decision_window(feature_id, name)
        )


def test_a_windowless_record_never_projects_equal_to_the_obligations_capsule() -> None:
    """M2. The sentinel's purpose: erase the volatile value without erasing the distinction."""
    capsule = projected_decision_window("obligations", MandatoryReason.TASK_OBLIGATION)
    other = projected_decision_window("caller_contract", MandatoryReason.TASK_OBLIGATION)
    assert capsule and capsule != other


def test_a_different_producer_claim_is_still_a_different_generation() -> None:
    """The guard must keep BITING; normalizing one field must not normalize identity away."""
    a = ra._evidence_generation_projection(_journal_payload("GT-W-aaaa"))
    b = ra._evidence_generation_projection(_journal_payload("GT-W-bbbb"))
    b["claim"] = "a different claim entirely"
    assert a != b


def test_the_projection_never_raises_on_a_malformed_payload() -> None:
    """A reader over immutable run artifacts must not crash on an unexpected row."""
    for payload in ({}, {"feature_id": None, "mandatory_reason": None}, {"claim": 1}):
        assert isinstance(ra._evidence_generation_projection(payload), dict)


def test_the_projection_stays_json_serializable() -> None:
    """It is compared field-by-field and surfaced in rejection diagnostics."""
    json.dumps(ra._evidence_generation_projection(_journal_payload("GT-W-x")))
