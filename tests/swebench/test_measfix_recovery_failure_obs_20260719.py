#!/usr/bin/env python3
"""MEAS-FIX (b5) TTD — the Gate-3/Gate-4 recovery timing RESIDUAL.

A governor ``recovery`` steer is delivered ONLY when the governor observes the agent is stuck
(a ``failure_obs``). The registry pins recovery's boundary event to ``failure_obs``
(``required_event('recovery') == 'failure_obs'``). But the grader-side ``_parse_timeline``
failure detector (``has_build_fail``) flags only EXPLICIT build-failure text in an observation;
it MISSES the governor's broader no-progress / stuck detection. When that happens the ONLY
``failure_obs`` boundary in the trajectory lands AFTER the recovery delivery, so
``_decision_open_index`` finds no boundary at-or-before delivery and the whole recovery
chronology collapses to ``decision_open_unresolved`` -> UNMEASURED. Both
``correct_rl_adhered_time`` (needs decision_open+commit) AND ``acknowledged``
(``pivoted_after_steer`` needs ``_measurable`` = delivery+open) fall through to ``None`` even
though the delivery is byte-proven and the trajectory carries the failure and the pivot act.

The fix reconciles the boundary set with the SEAM's authority (the SAME B-BND doctrine already
applied to submit interceptions (b1) and post_edit edits (b3)): a delivered row whose registered
boundary event is ``failure_obs`` opens a ``failure_obs`` boundary at its OWN iteration, mapped
through the SAME iteration -> tool_ordinal join the delivery uses. It is registry-driven (never
keyed on the class string 'recovery'), additive (never removes a _parse_timeline failure_obs),
and fail-closed (a non-failure_obs producer, or an iteration with no tool message, adds nothing).

Observed artifact (RED before / GREEN after): run4_smoke_29694879462, matplotlib__matplotlib-29721
— recovery delivered at iteration 51 (msg 103); _parse_timeline's only failure_obs is at msg 233
(> 103); pre-fix time=None ack=None; post-fix time=False (STEP_BEHIND — the agent had touched the
entity earlier) ack=True (a real pivot mutation at msg 104). ZERO previously-measured verdicts
change.

Run: PYTHONPATH=scripts/swebench:src pytest tests/swebench/test_measfix_recovery_failure_obs_20260719.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# scripts/swebench is not a package and is not installed; `chronology_extract` (and
# `receipt_predicates`, imported lazily below) resolve only when that directory is on
# sys.path. The `Run:` line above sets PYTHONPATH, but a bare `pytest <this file>` does not,
# so the file used to be a collection ERROR whenever it ran alone. Same bootstrap as the
# working neighbours (tests/swebench/test_gt_feature_metrics_128.py).
for _p in (Path(__file__).resolve().parents[2] / "scripts" / "swebench",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import chronology_extract as ce  # noqa: E402
from groundtruth.runtime import fact_registry as fr  # noqa: E402
from groundtruth.runtime.chronological_adjudication import UNMEASURED


# --------------------------------------------------------------------------- #
# fixtures — a minimal [user, (assistant, tool)*n] message list. No command text ->
# _parse_timeline yields no trajectory boundaries, so any failure_obs observed is purely the
# SEAM-derived one under test (exactly the run-4 shape: a stuck-observation with no build-fail
# text in it, so has_build_fail is False).
# --------------------------------------------------------------------------- #
def _messages(n_tools: int) -> list[dict]:
    msgs: list[dict] = [{"role": "user", "content": "task"}]
    for _ in range(n_tools):
        msgs.append({"role": "assistant", "content": "act"})
        msgs.append({"role": "tool", "content": "obs (no build-fail text)"})
    return msgs


def _recovery_row(iteration: int) -> dict:
    """The run-4 recovery delivery row shape (event_type is the coarse hook phase 'post_view';
    the registry — not the row's event_type — is what pins the failure_obs boundary)."""
    return {
        "layer": "governor.recovery",
        "evidence_type": "recovery",
        "event_type": "post_view",
        "actual_event": "failure_obs",
        "iteration": iteration,
        "outcome": "delivered",
        "chars_delivered": 443,
    }


# =========================================================================== #
# b5 — the recovery steer is the seam's authority that a failure_obs occurred
# =========================================================================== #
def test_b5_recovery_row_opens_failure_obs_boundary():
    # RED before fix: the recovery row's iteration seeds NO failure_obs boundary (the crafted
    # observation has no build-fail text, so _parse_timeline adds none), so decision_open is None.
    msgs = _messages(6)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [_recovery_row(3)]
    b = ce._boundary_indices(msgs, rows, toi)
    assert toi[3] in b["failure_obs"], (
        "a delivered governor recovery steer must open a failure_obs boundary at its iteration"
    )


def test_b5_resolves_decision_open_for_a_recovery_parse_timeline_missed():
    # the exact run-4 collapse: the ONLY _parse_timeline failure_obs is AFTER the delivery, so
    # decision_open finds nothing at-or-before delivery -> UNMEASURED. The seam boundary fixes it.
    msgs = _messages(6)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [_recovery_row(3)]
    required = fr.required_event("recovery")  # failure_obs
    delivery = toi[3]
    # pre-fix boundary set (no ledger reconciliation) has no failure_obs <= delivery.
    b_noseam = ce._boundary_indices(msgs)
    assert ce._decision_open_index(required, delivery, b_noseam) is None
    # post-fix: the seam-reconciled boundary resolves decision_open exactly at the delivery.
    b = ce._boundary_indices(msgs, rows, toi)
    assert ce._decision_open_index(required, delivery, b) == delivery


def test_b5_byte_identical_without_ledger_args():
    # the fair_probe path calls _boundary_indices(messages) with no ledger rows — must be
    # unchanged: no seam failure_obs is ever added there.
    msgs = _messages(6)
    b_old = ce._boundary_indices(msgs)
    assert b_old["failure_obs"] == []


def test_b5_boundary_is_at_or_before_the_delivery_it_answers():
    # the boundary is the SAME iteration->tool_ordinal join the delivery uses, so it is never
    # after the delivery (fail-closed, never synthesized).
    msgs = _messages(6)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [_recovery_row(4)]
    b = ce._boundary_indices(msgs, rows, toi)
    assert ce._decision_open_index("failure_obs", toi[4], b) == toi[4]


# --------------------------------------------------------------------------- #
# biting mutations — each must FAIL if the fix is reverted or weakened.
# --------------------------------------------------------------------------- #
def test_b5_MUTATION_non_failure_obs_class_never_seeds_failure_obs():
    # a non-recovery delivered row (its registered boundary is NOT failure_obs) must add nothing.
    msgs = _messages(6)
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [{"layer": "l3.caller", "evidence_type": "caller_contract",
             "event_type": "post_edit", "iteration": 3, "outcome": "delivered"}]
    b = ce._boundary_indices(msgs, rows, toi)
    assert toi[3] not in b["failure_obs"], (
        "only a registered failure_obs producer may open a failure_obs boundary"
    )


def test_b5_MUTATION_registry_key_not_the_class_string():
    # the guard is registry-driven: exactly the classes whose required_event is failure_obs.
    # Today that is precisely {recovery} — assert the registry contract the fix relies on.
    failure_obs_types = [et for et in fr.REGISTRY if fr.required_event(et) == "failure_obs"]
    assert failure_obs_types == ["recovery"]
    assert fr.required_event("recovery") == "failure_obs"


def test_b5_MUTATION_iteration_without_tool_message_is_skipped():
    # fail-closed: an iteration with no tool message maps to None and seeds no boundary.
    msgs = _messages(3)  # tool ordinals 1..3 only
    toi = ce._tool_ordinal_to_index(msgs)
    rows = [_recovery_row(99)]  # no 99th tool message
    b = ce._boundary_indices(msgs, rows, toi)
    assert b["failure_obs"] == []


# =========================================================================== #
# end-to-end on the REAL run-4 artifact (skips when the local run dir is absent).
# =========================================================================== #
_ART = "D:/gt_runs/run4_smoke_29694879462/art/ll-full-matplotlib__matplotlib-29721"


@pytest.mark.skipif(
    not os.path.isdir(_ART), reason="run4 smoke artifacts not present locally"
)
def test_b5_e2e_matplotlib_recovery_becomes_measured():
    from receipt_predicates import acknowledgment_by_fact_class

    traj = json.load(
        open(os.path.join(_ART, "mini-swe-agent.trajectory.json"), encoding="utf-8")
    )
    rows = [
        json.loads(line)
        for line in open(
            os.path.join(_ART, "gt_runtime_ledger_matplotlib__matplotlib-29721.jsonl"),
            encoding="utf-8",
        )
        if line.strip()
    ]
    chronos = list(ce.extract_chronologies(traj, rows).values())
    chronos.extend(ce.extract_block_chronologies(traj, rows))
    recovery = [ec for ec in chronos if ec.fact_class == "recovery"]
    assert recovery, "matplotlib-29721 must carry a delivered recovery row"
    # every recovery row now binds its decision_open (was decision_open_unresolved -> UNMEASURED).
    for ec in recovery:
        assert ec.chronology.decision_open_index is not None
        assert ec.timing_verdict != UNMEASURED
        assert ec.unmeasured_reason is None
    ack = acknowledgment_by_fact_class(
        chronos, messages=traj.get("messages"), ledger_rows=rows
    )
    # both gates now carry a measured value instead of None (fail-through).
    verdict, correct_time = ce._class_verdict([e.timing_verdict for e in recovery])
    assert correct_time is not None, "correct_rl_adhered_time must now be measured (not None)"
    assert ack.get("recovery") is not None, "acknowledged must now be measured (not None)"
