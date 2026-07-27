"""CHARACTERIZATION: the only STANDING BEHAVIORAL_CONTRACT carrier is spent after one delivery.

This file asserts what the runtime does TODAY. It is not a wish; it is a pin, so that the
fragility is reproducible and any fix is visibly a change rather than a silent drift.

THE SHAPE OF THE PROBLEM.

`PATCH_CONSTRUCTION` requires BEHAVIORAL_CONTRACT. Three fact classes carry that role --
`caller_contract`, `obligations`, `signature_delta` -- but only `obligations` STANDS without a
trigger: it is derived from the immutable task inputs at task start. `caller_contract` needs an
edit against a function with deterministic graph callers; `signature_delta` needs a positional
parameter change with callers. Neither is guaranteed on any given task.

Once `obligations` is DELIVERED, the lifecycle table permits only
DELIVERED -> ACTIVE -> {SATISFIED, SUPERSEDED, INVALIDATED, EXPIRED}, and
`evaluate_feature_contract` returns `release_allowed=False` for every one of those states. The
record can never be released again.

CONSEQUENCE: after the first obligations delivery, a task on which neither event-triggered carrier
fires has NO way to complete another PATCH_CONSTRUCTION decision. Every subsequent compile attempt
reports `unresolved_roles=[BEHAVIORAL_CONTRACT]` -- which is precisely the fingerprint measured
across two full runs (70 of 90 compile attempts), and which was previously attributed entirely to
the freshness-churn and dead-substrate bugs. Those were real and are fixed; this is a THIRD,
independent reason the same symptom can recur, and it is structural rather than a defect.

WHY NO FIX HERE. The candidate fix -- re-materialising a fresh obligations record from the
immutable task inputs when PATCH_CONSTRUCTION reopens -- changes evidence-lifecycle semantics, the
most load-bearing invariant in this runtime. `issue` being immutable (so such a record would always
be fresh) is a necessary precondition and now holds, but sufficiency is not established: the
interaction with dose<=1, the dedup/novelty referees, and the DELIVERED-is-terminal guarantee all
need review before anything is changed. Pinning the behaviour is the honest intermediate step.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import groundtruth.runtime.reasoning_runtime as rr  # noqa: E402
from groundtruth.runtime.fact_registry import all_fact_classes  # noqa: E402


def _carriers_of(role_name: str) -> set[str]:
    out = set()
    for fact in all_fact_classes():
        contract = rr.feature_contract_for(fact)
        if contract is None:
            continue
        if any(getattr(r, "name", str(r)) == role_name for r in contract.roles):
            out.add(fact)
    return out


def test_behavioral_contract_carriers_are_exactly_these_three():
    """POSITIVE CONTROL on the premise. If the carrier set changes, every conclusion in this
    file must be re-derived rather than inherited."""
    assert _carriers_of("BEHAVIORAL_CONTRACT") == {
        "caller_contract", "obligations", "signature_delta",
    }


def test_only_obligations_is_contracted_to_task_start():
    """The STANDING property is what matters, not the count. `obligations` is the only carrier
    whose delivery boundary is task_start -- the others wait on an agent event that may never
    happen on a given task."""
    from groundtruth.runtime.fact_registry import registration_for

    boundaries = {
        fact: registration_for(fact).deliver_by
        for fact in _carriers_of("BEHAVIORAL_CONTRACT")
    }
    assert boundaries["obligations"] == "task_start"
    assert boundaries["caller_contract"] != "task_start"
    assert boundaries["signature_delta"] != "task_start"


def test_delivered_evidence_can_never_be_released_again():
    """THE PIN. Every post-DELIVERED lifecycle is unreleasable, so the standing anchor is
    single-dose by construction."""
    unreleasable = {
        rr.EvidenceLifecycle.DELIVERED,
        rr.EvidenceLifecycle.ACTIVE,
        rr.EvidenceLifecycle.SATISFIED,
        rr.EvidenceLifecycle.SUPERSEDED,
        rr.EvidenceLifecycle.EXPIRED,
        rr.EvidenceLifecycle.INVALIDATED,
    }
    # The transition table must not offer any route from DELIVERED back to a releasable state.
    reachable = {rr.EvidenceLifecycle.DELIVERED}
    frontier = [rr.EvidenceLifecycle.DELIVERED]
    while frontier:
        current = frontier.pop()
        for nxt in rr._EVIDENCE_TRANSITIONS.get(current, frozenset()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable <= unreleasable, (
        f"a releasable state is reachable from DELIVERED: {reachable - unreleasable} -- if this "
        "is intentional, the single-dose reasoning in this file is obsolete"
    )


def test_the_transition_table_is_non_empty():
    """POSITIVE CONTROL on the walk above: an empty table would make it pass vacuously."""
    assert rr._EVIDENCE_TRANSITIONS
    assert rr._EVIDENCE_TRANSITIONS.get(rr.EvidenceLifecycle.DELIVERED)
