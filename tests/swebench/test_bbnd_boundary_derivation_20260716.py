#!/usr/bin/env python3
"""B-BND TTD — the four Gate-3 timing RESIDUALS (b1-b4). Each is RED before the fix and
GREEN after, with >=2 biting mutations that must FAIL if the fix is reverted/weakened.

The fixes make timing MEASURABLE for classes previously left None, WITHOUT weakening
fail-closed: every added boundary is a REAL recorded seam event, and no synthetic index is
minted. Residuals:
  b1 submit_refusal — a mid-run submit INTERCEPTION opens a ``submit`` boundary (from the
     ``submit_gate`` ledger row), not only the terminal exit.
  b2 covering_red   — the SEAM's executed covering RED is delivered at EDIT time
     (edit_result boundary), not the canonical test_result.
  b3 syntax_result  — an edit the SEAM's post_edit hook recorded opens an ``edit_result``
     boundary even when the grader's _parse_timeline edit detection missed it.
  b4 recovery       — a pivot decision is COMMITTED by the next agent ACT (any action), not a
     repository mutation.

Run: PYTHONPATH=scripts/swebench:src pytest tests/swebench/test_bbnd_boundary_derivation_20260716.py -q
"""
import pytest

import chronology_extract as ce
from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime.chronological_adjudication import (
    ON_TIME,
    UNMEASURED,
    WRONG_EVENT,
    Chronology,
    adjudicate,
)


# --------------------------------------------------------------------------- #
# fixtures — a minimal message list with N tool observations for the tool_ordinal join.
# --------------------------------------------------------------------------- #
def _messages(n_tools: int) -> list[dict]:
    """[user, then (assistant, tool) * n]. Tool ordinal k -> the message index of the k-th
    tool message. No command text -> _parse_timeline yields no trajectory boundaries, so any
    boundary observed is purely the SEAM-derived one under test."""
    msgs: list[dict] = [{"role": "user", "content": "task"}]
    for _ in range(n_tools):
        msgs.append({"role": "assistant", "content": "act"})
        msgs.append({"role": "tool", "content": "obs"})
    return msgs


def _assistant(cmd: str) -> dict:
    """An assistant message whose bash command is `cmd` (mini-swe-agent tool_calls shape)."""
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "bash",
                                         "arguments": '{"command": ' + repr(cmd).replace("'", '"') + "}"}}]}


# =========================================================================== #
# b1 — submit interception boundary from the real submit_gate ledger row
# =========================================================================== #
def test_b1_submit_gate_row_opens_submit_boundary():
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "submit_gate", "event_type": "", "iteration": 3}]
    b = ce._boundary_indices(msgs, rows, toi)
    assert toi[3] in b["submit"], "a submit_gate interception must open a submit boundary"


def test_b1_byte_identical_without_ledger_args():
    # the fair_probe path calls _boundary_indices(messages) — must be unchanged (no interception).
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    b_old = ce._boundary_indices(msgs)
    assert toi[3] not in b_old["submit"]
    # the only submit boundary is the terminal last-observation.
    assert b_old["submit"] == [max(toi.values())]


def test_b1_derived_boundary_is_at_or_before_the_delivery_it_answers():
    # the interception boundary is the same iteration->tool_ordinal join the delivery uses,
    # so it is guaranteed <= any delivery of that iteration (fail-closed, never synthesized).
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "submit_gate", "event_type": "", "iteration": 4}]
    b = ce._boundary_indices(msgs, rows, toi)
    # a submit_refusal delivered at iteration 4 resolves its decision_open from this boundary.
    assert ce._decision_open_index("submit", toi[4], b) == toi[4]


def test_b1_MUTATION_wrong_interception_layer_breaks_it():
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "submit_gate", "event_type": "", "iteration": 3}]
    saved = ce._SUBMIT_INTERCEPTION_LAYER
    try:
        ce._SUBMIT_INTERCEPTION_LAYER = "not_the_gate"  # mutate the interception identity
        b = ce._boundary_indices(msgs, rows, toi)
        assert toi[3] not in b["submit"], "mutating the interception layer must drop the boundary"
    finally:
        ce._SUBMIT_INTERCEPTION_LAYER = saved


def test_b1_MUTATION_non_gate_layer_row_never_seeds_submit():
    # a non-interception row (e.g. a contract delivery) must never open a submit boundary.
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "l3.contract", "event_type": "post_edit", "iteration": 3}]
    b = ce._boundary_indices(msgs, rows, toi)
    assert toi[3] not in b["submit"]


# =========================================================================== #
# b3 — an edit the SEAM recorded (post_edit ledger row) opens an edit_result boundary
# =========================================================================== #
def test_b3_post_edit_row_opens_edit_result_boundary():
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "edit.syntax", "event_type": "post_edit", "iteration": 2}]
    b = ce._boundary_indices(msgs, rows, toi)
    assert toi[2] in b["edit_result"]
    assert toi[2] in b["first_view_edit"]  # the composite companion boundary


def test_b3_resolves_decision_open_for_a_syntax_result_the_grader_missed():
    # syntax_result delivered at iteration 2's edit; the grader's _parse_timeline saw no edit,
    # but the seam's post_edit row is authoritative -> decision_open resolves.
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "edit.syntax", "event_type": "post_edit", "iteration": 2}]
    b = ce._boundary_indices(msgs, rows, toi)
    required = fr.required_event("syntax_result")  # edit_result
    assert ce._decision_open_index(required, toi[2], b) == toi[2]


def test_b3_MUTATION_empty_phase_map_breaks_it():
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "edit.syntax", "event_type": "post_edit", "iteration": 2}]
    saved = ce._SEAM_PHASE_TO_BOUNDARIES
    try:
        ce._SEAM_PHASE_TO_BOUNDARIES = {}  # mutate: seam phases no longer open boundaries
        b = ce._boundary_indices(msgs, rows, toi)
        assert toi[2] not in b["edit_result"]
    finally:
        ce._SEAM_PHASE_TO_BOUNDARIES = saved


def test_b3_MUTATION_unmapped_phase_is_closed_not_fuzzy():
    # only the explicit phases in the closed table seed boundaries; an unmapped phase adds none.
    msgs = _messages(5)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "verify", "event_type": "review_transition", "iteration": 2}]
    b = ce._boundary_indices(msgs, rows, toi)
    assert toi[2] not in b["edit_result"]


# =========================================================================== #
# b4 — recovery commits its pivot by the next agent ACT, not a mutation
# =========================================================================== #
def test_b4_recovery_commits_on_next_nonmutating_action():
    # open at msg 0; the agent's next act (msg 1) is a passive re-read (no mutation).
    msgs = [{"role": "tool", "content": "steer"}, _assistant("grep -rn foo src/")]
    assert ce._decision_commit_index(msgs, 0, "recovery") == 1


def test_b4_nonpivot_class_still_requires_a_mutation():
    # syntax_result keeps the strict mutation predicate: a re-read is NOT a commit.
    msgs = [{"role": "tool", "content": "x"}, _assistant("cat src/foo.py")]
    assert ce._decision_commit_index(msgs, 0, "syntax_result") is None


def test_b4_coherence_collapse_alias_resolves_to_recovery_pivot():
    msgs = [{"role": "tool", "content": "x"}, _assistant("grep -rn foo src/")]
    assert ce._decision_commit_index(msgs, 0, "coherence_collapse") == 1


def test_b4_MUTATION_empty_pivot_set_reverts_recovery_to_mutation_only():
    msgs = [{"role": "tool", "content": "steer"}, _assistant("grep -rn foo src/")]
    saved = ce._PIVOT_COMMIT_CLASSES
    try:
        ce._PIVOT_COMMIT_CLASSES = frozenset()  # mutate: no class is a pivot commit
        assert ce._decision_commit_index(msgs, 0, "recovery") is None
    finally:
        ce._PIVOT_COMMIT_CLASSES = saved


def test_b4_MUTATION_submit_refusal_deliberately_excluded_from_pivot():
    # submit_refusal must NOT be a pivot commit — its decision commits on the next REPAIR
    # (mutation), not any keystroke; a non-mutation next act must leave it uncommitted.
    assert not ce._is_pivot_commit("submit_refusal")
    msgs = [{"role": "tool", "content": "refused"}, _assistant("git diff")]
    assert ce._decision_commit_index(msgs, 0, "submit_refusal") is None


# =========================================================================== #
# b2 — the SEAM's covering RED is an EDIT-time, on-time-by-construction fact
# =========================================================================== #
def test_b2_covering_red_boundary_is_edit_result():
    assert fr.required_event("covering_red") == "edit_result"
    assert fr.earliest_event_for("covering_red") == "edit_result"


def test_b2_gateway_covering_verdict_keeps_canonical_test_result():
    # the split is by evidence_type: the gateway's test-event wrap ships 'covering_verdict'
    # and MUST stay at the canonical test_result boundary (not broken by the seam override).
    assert fr.required_event("covering_verdict") == "test_result"
    assert not fr.is_reactive("covering_verdict")


def test_b2_covering_red_uses_fixed_edit_boundary_not_reactive_exemption():
    assert not fr.is_reactive("covering_red")


def test_b2_decision_open_resolves_from_edit_boundary_only():
    # the covering RED rides a post_edit observation; only an edit_result boundary exists
    # before delivery (the agent ran no test). The override makes decision_open resolve.
    boundaries = {ev: [] for ev in fr.EVENTS}
    boundaries["edit_result"] = [10]
    required = fr.required_event("covering_red")  # edit_result (override)
    assert ce._decision_open_index(required, 12, boundaries) == 10


def test_b2_adjudicate_measures_the_saved_row_shape():
    # The covering runner executes internally, but its result is delivered in the post-edit
    # observation. The saved lineage therefore names that real observation boundary directly.
    ch = Chronology(decision_open_index=5, delivery_index=6, decision_commit_index=7,
                    native_acquisition_index=None, acknowledgment_index=None, action_index=None)
    verdict = adjudicate(evidence_type="covering_red", actual_event="edit_result",
                         delivery_seal="0" * 16, chronology=ch).timing_verdict
    assert verdict == ON_TIME


def test_b2_MUTATION_stamping_internal_test_event_rejects_the_saved_row():
    # A producer-internal test execution is not the model observation boundary. Reverting the
    # saved event to test_result must fail closed against covering_red's fixed edit boundary.
    ch = Chronology(decision_open_index=5, delivery_index=6, decision_commit_index=7,
                    native_acquisition_index=None, acknowledgment_index=None, action_index=None)
    verdict = adjudicate(evidence_type="covering_red", actual_event="test_result",
                         delivery_seal="0" * 16, chronology=ch).timing_verdict
    assert verdict == WRONG_EVENT


def test_b2_MUTATION_dropping_boundary_override_loses_decision_open():
    # without the edit_result override the required boundary reverts to test_result; a covering
    # RED delivered at the edit (only an edit_result boundary present) can no longer resolve
    # decision_open -> the exact None the fix removes.
    boundaries = {ev: [] for ev in fr.EVENTS}
    boundaries["edit_result"] = [10]
    saved = dict(fr._EVIDENCE_TYPE_DELIVER_BY)
    try:
        fr._EVIDENCE_TYPE_DELIVER_BY.pop("covering_red", None)
        required = fr.required_event("covering_red")  # reverts to canonical test_result
        assert required == "test_result"
        assert ce._decision_open_index(required, 12, boundaries) is None
    finally:
        fr._EVIDENCE_TYPE_DELIVER_BY.clear()
        fr._EVIDENCE_TYPE_DELIVER_BY.update(saved)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
