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


# Roles DECLARED in the EvidenceRole enum that no fact class currently carries. This list is
# the finding, not the excuse: a role a DecisionContext lists as useful but nothing can supply
# is a permanently unfillable slot. Both are `useful`, never `required`, so neither blocks a
# decision from completing today -- which is exactly why they went unnoticed. Shrink this list;
# never grow it without saying why here.
_KNOWN_CARRIER_LESS_ROLES = frozenset({
    "MATERIAL_UNCERTAINTY",   # listed useful by 4 contexts; zero producers
    "HISTORICAL_SUPPORT",     # cochange is an internal ranking prior, never delivered
})


def test_every_role_a_context_needs_has_at_least_one_carrier_somewhere():
    """No DECLARED role may be carrier-less across the whole registry, except a named few.

    REWRITTEN 2026-07-27 -- THE PREVIOUS VERSION WAS VACUOUS AND COULD NEVER FAIL. It built
    `carriers` with `setdefault(role, []).append(fact)`, so every key it created was populated
    by the very append that created it; `[role for role, facts in carriers.items() if not
    facts]` was therefore ALWAYS `[]`. It asserted a tautology while reading as coverage of a
    fatal invariant -- and it sat directly above two roles that genuinely have zero carriers,
    reporting green.

    The repair is to iterate the DECLARED role universe (the `EvidenceRole` enum) rather than
    the set of roles that happen to appear on a contract. Only then can the difference between
    the two be observed, which is the entire question.
    """
    carriers: dict[str, list[str]] = {}
    for fact in sorted(all_fact_classes()):
        contract = rr.feature_contract_for(fact)
        if contract is None:
            continue
        for role in contract.roles:
            carriers.setdefault(getattr(role, "name", str(role)), []).append(fact)

    declared = {role.name for role in rr.EvidenceRole}
    assert declared, "POSITIVE CONTROL: the EvidenceRole enum resolved empty"
    assert carriers, "POSITIVE CONTROL: no roles resolved from any contract"
    # NEGATIVE CONTROL for the rewrite: the two sets must actually DIFFER, or this test has
    # quietly become the tautology it replaced.
    assert declared - set(carriers), (
        "declared roles and carried roles now coincide -- if that is a real fix, delete "
        "_KNOWN_CARRIER_LESS_ROLES; if not, this test has gone vacuous again"
    )

    uncarried = declared - set(carriers)
    assert uncarried == set(_KNOWN_CARRIER_LESS_ROLES), (
        f"the carrier-less role set moved: now {sorted(uncarried)}, "
        f"expected {sorted(_KNOWN_CARRIER_LESS_ROLES)}. A NEW carrier-less role is a "
        f"permanently unfillable slot; a role that GAINED a carrier should be removed "
        f"from the list."
    )
    # And no role a context REQUIRES may ever be in that set -- that would be fatal, not
    # merely wasteful. Required roles today: TARGET_IDENTITY, BEHAVIORAL_CONTRACT, VALIDATION,
    # CONTRADICTION, TERMINAL_ASSURANCE.
    for required in (
        "TARGET_IDENTITY", "BEHAVIORAL_CONTRACT", "VALIDATION",
        "CONTRADICTION", "TERMINAL_ASSURANCE",
    ):
        assert carriers.get(required), (
            f"{required} is a REQUIRED role of some DecisionContext and has no carrier; "
            f"that decision can never complete"
        )
