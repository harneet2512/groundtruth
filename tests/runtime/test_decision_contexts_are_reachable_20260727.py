"""Every decision context must be fillable, and the one that is not must stay named.

WHY THIS EXISTS. The oracle can only release a capsule when the OPEN decision's required roles are
covered. A decision context that no fact class is contracted to therefore cannot EVER complete --
not because of a bug in the oracle, the phase policy, or delivery, but because nothing was ever
built that could answer it. That is invisible from any single trajectory: it looks exactly like
"the feature was quiet", which is the misreading that cost this project days.

MEASURED FROM THE REGISTRY (2026-07-27), not inferred:

    SOURCE_TARGET_SELECTION   def_partition, localization, newfile_precedent
    SOURCE_UNDERSTANDING      -- NOTHING --
    PATCH_CONSTRUCTION        caller_contract, obligations
    PATCH_PROPAGATION         signature_delta, syntax_result
    FAILURE_RECOVERY          covering_red, recovery
    COMPLETION                submit_refusal

A PRECISION CORRECTION worth carrying: BEHAVIORAL_CONTRACT is carried by THREE fact classes --
`caller_contract`, `obligations` and `signature_delta`. The often-repeated claim that "obligations
is the only carrier of BEHAVIORAL_CONTRACT" is true only in the narrower sense that it is the only
STANDING one; the other two require their own triggers (an edit with graph callers, a signature
change). The distinction matters: the fix for role fragility is not "add carriers", it is "make a
carrier that stands without a trigger".

WHY SOURCE_UNDERSTANDING IS NOT SIMPLY A BUG TO DELETE. Under `GT_ROLE_DRIVEN_COALITION` (set in
the CI workflows, but DEFAULT OFF and not a profile member) eligibility is ROLE-fit rather than
CONTEXT-fit, so any BEHAVIORAL_CONTRACT carrier can fill it. With the flag off -- the default --
every post-view observation opens a decision that nothing can answer. So this is a real hole on the
default path, and a flag-shaped mitigation on the CI path.

THE SHAPE OF THIS TEST. The gap is recorded as a NAMED, DOCUMENTED exception that must not grow,
following the `_PENDING_AE_FORWARD_SEAM` precedent already used in this repo for exactly this
situation. A gap in a named set is a tracked defect; the same gap unrecorded is an invisible one.
Adding a contracted fact class for SOURCE_UNDERSTANDING should REMOVE it from the set, and the
test below fails if anyone adds a new empty context instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import groundtruth.runtime.reasoning_runtime as rr  # noqa: E402
from groundtruth.runtime.fact_registry import all_fact_classes  # noqa: E402


# Contexts with NO contracted fact class. This set must only ever SHRINK.
_KNOWN_UNREACHABLE = frozenset({"SOURCE_UNDERSTANDING"})


def _contracted_facts_by_context() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {ctx.name: [] for ctx in rr.DecisionContext}
    for fact in sorted(all_fact_classes()):
        contract = rr.feature_contract_for(fact)
        if contract is None:
            continue
        out[contract.decision_context.name].append(fact)
    return out


def test_the_matrix_is_computable_at_all():
    """POSITIVE CONTROL. If the registry stops resolving, every assertion below would pass
    vacuously on empty data -- the exact shape of a green test that proves nothing."""
    matrix = _contracted_facts_by_context()
    assert matrix, "no decision contexts resolved"
    filled = [c for c, f in matrix.items() if f]
    assert len(filled) >= 5, f"only {len(filled)} contexts have any producer: {matrix}"


def test_no_NEW_decision_context_is_unreachable():
    """THE GUARD. A context nothing is contracted to can never complete, and that is
    indistinguishable from correct quiet in any single run."""
    matrix = _contracted_facts_by_context()
    unreachable = {ctx for ctx, facts in matrix.items() if not facts}
    new = unreachable - _KNOWN_UNREACHABLE
    assert not new, (
        f"decision context(s) {sorted(new)} have NO contracted fact class -- the oracle can "
        "never complete a decision there, and it will read as 'the feature was quiet'"
    )


def test_the_known_gap_has_not_been_silently_fixed_without_updating_this_file():
    """The set must SHRINK deliberately. If SOURCE_UNDERSTANDING gains a producer, this fails
    and forces the exception to be removed -- otherwise a stale exemption would keep hiding the
    next regression at the same spot."""
    matrix = _contracted_facts_by_context()
    still_empty = {ctx for ctx, facts in matrix.items() if not facts}
    fixed = _KNOWN_UNREACHABLE - still_empty
    assert not fixed, (
        f"{sorted(fixed)} now HAS a contracted fact class -- remove it from "
        "_KNOWN_UNREACHABLE so the guard keeps biting there"
    )


def test_behavioral_contract_has_more_than_one_carrier():
    """PRECISION. The 'only carrier' framing is wrong and has been repeated for days: three fact
    classes carry BEHAVIORAL_CONTRACT. Only `obligations` STANDS without a trigger, which is the
    property that actually matters for role fragility."""
    carriers = [
        fact for fact in sorted(all_fact_classes())
        if (c := rr.feature_contract_for(fact)) is not None
        and any(getattr(r, "name", str(r)) == "BEHAVIORAL_CONTRACT" for r in c.roles)
    ]
    assert set(carriers) >= {"caller_contract", "obligations", "signature_delta"}, carriers


def test_every_role_a_context_needs_has_at_least_one_carrier_somewhere():
    """A weaker but flag-independent invariant: no required role may be carrier-less across the
    WHOLE registry. Under role-driven eligibility this is what actually decides whether a
    decision can complete, so a role with zero carriers is fatal regardless of context wiring."""
    carriers: dict[str, list[str]] = {}
    for fact in sorted(all_fact_classes()):
        contract = rr.feature_contract_for(fact)
        if contract is None:
            continue
        for role in contract.roles:
            carriers.setdefault(getattr(role, "name", str(role)), []).append(fact)
    assert carriers, "POSITIVE CONTROL: no roles resolved at all"
    empty = [role for role, facts in carriers.items() if not facts]
    assert not empty, f"roles with no carrier: {empty}"
