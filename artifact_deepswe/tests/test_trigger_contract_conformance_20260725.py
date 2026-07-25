"""TRIGGER-CONTRACT CONFORMANCE — the deterministic bridge from offline code to online proof.

The problem this exists to solve: we were HOPING the 17 features fire correctly online. A run then
either showed a ledger row or did not, and neither outcome distinguished "the feature is broken"
from "its trigger never occurred". That is guessing, not proving.

`fact_registry` already declares the contract per class — `deliver_by` (the observation boundary at
which the fact is owed), `max_dose`, `receipt_predicate`, `ack_expected`. This suite asserts the
contract is COMPLETE, INTERNALLY CONSISTENT, and MATCHES the runtime, so that "will it fire online"
becomes a property checkable offline rather than an outcome discovered after spending a run.

Measured facts this encodes (2026-07-25):
  * the 10 DIRECT FACT classes map to 8 DISTINCT boundaries — only TWO genuine collisions exist
    (search_result: def_partition+localization; edit_result: signature_delta+syntax_result). The
    static global priority table therefore manufactures competition the contract never asked for,
    which is what vetoes the low-ranked features under a single global dose.
  * `max_dose` is already PER CLASS (localization "small", obligations unbounded, most 1), so the
    blanket one-dose-per-observation is an arbiter choice, not the spec.
"""
from __future__ import annotations
import pytest

from groundtruth.runtime.fact_registry import all_fact_classes, registration_for

# The 10 FACT classes that are DIRECT features (cochange_prior is the INFLUENCE row, graded on its
# own terminal and never a DIRECT feature — CLAUDE.md §6).
_DIRECT_FACTS = {
    "caller_contract", "covering_red", "def_partition", "localization", "newfile_precedent",
    "obligations", "recovery", "signature_delta", "submit_refusal", "syntax_result",
}
# Every boundary the harness can synthesize. A deliver_by outside this set cannot be proven
# offline, which makes the feature un-provable without spending a run.
_KNOWN_BOUNDARIES = {
    "task_start", "search_result", "failed_search", "file_view", "first_view_edit",
    "edit_result", "test_result", "failure_obs", "submit",
}


def test_every_direct_fact_is_registered():
    missing = _DIRECT_FACTS - set(all_fact_classes())
    assert not missing, f"DIRECT features with no trigger contract: {sorted(missing)}"


@pytest.mark.parametrize("fact", sorted(_DIRECT_FACTS))
def test_each_fact_declares_a_synthesizable_boundary(fact):
    """Un-synthesizable boundary => the feature cannot be proven offline, only hoped for online."""
    db = str(registration_for(fact).deliver_by)
    assert db in _KNOWN_BOUNDARIES, f"{fact}: deliver_by={db!r} is not a synthesizable boundary"


@pytest.mark.parametrize("fact", sorted(_DIRECT_FACTS))
def test_each_fact_declares_an_acknowledgement_contract(fact):
    """`acknowledged` is the one gate that cannot be enforced — so it must at least be DEFINED
    per class, otherwise it is unfalsifiable rather than merely empirical."""
    reg = registration_for(fact)
    assert reg.receipt_predicate, f"{fact}: no receipt_predicate — acknowledgement is undefined"
    assert reg.ack_expected is not None, f"{fact}: ack_expected unset"


@pytest.mark.parametrize("fact", sorted(_DIRECT_FACTS))
def test_each_fact_declares_a_producer_and_renderer(fact):
    """A contract without a producer cannot fire; without a renderer it cannot reach the model."""
    reg = registration_for(fact)
    assert reg.producer, f"{fact}: no producer declared"
    assert reg.native_renderer, f"{fact}: no native_renderer — cannot become model-facing bytes"


def test_boundary_collisions_are_exactly_the_two_measured_ones():
    """PINS the finding that drives specificity ranking. If a third collision appears, the
    arbiter's contention assumptions changed and the ranking design must be revisited."""
    by_boundary: dict[str, list[str]] = {}
    for fact in sorted(_DIRECT_FACTS):
        by_boundary.setdefault(str(registration_for(fact).deliver_by), []).append(fact)
    collisions = {b: fs for b, fs in by_boundary.items() if len(fs) > 1}
    assert collisions == {
        "search_result": ["def_partition", "localization"],
        "edit_result": ["signature_delta", "syntax_result"],
    }, f"boundary collision set changed: {collisions}"


def test_max_dose_is_per_class_not_globally_one():
    """The blanket <=1 dose is an ARBITER choice; the contract already varies per class."""
    doses = {f: str(registration_for(f).max_dose) for f in sorted(_DIRECT_FACTS)}
    assert len(set(doses.values())) > 1, (
        "every class now declares the same max_dose — the per-class contract has been flattened, "
        f"which would make the global one-dose veto correct-by-accident: {doses}"
    )
