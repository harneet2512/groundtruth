"""#30 step 2 — a deliberate measurement holdout is a terminal state of its OWN.

WHY A NEW STATE AT ALL. The shadow-holdout arm compiles a capsule and then deliberately does
NOT send it, so the coin's decision can be measured. Neither existing terminal fits:

  * `CANCELLED` maps from provider status "cancelled" -- it means THE PROVIDER cancelled, not
    that GT chose to withhold.
  * `RESPONSE_DISCARDED` is reached via `record_delivery_failure` and is about the RESPONSE,
    not the capsule.

Both are members of terminal-FAILURE sets. Reusing either would book a measurement decision as
a delivery FAILURE, corrupting failure accounting and the release gate -- and "GT quality must
never block a task" cuts the same way: a measurement arm must never look like a defect.

THE MEMBERSHIP DECISIONS ARE THE SUBSTANCE, and they are NOT uniform. Each flat predicate set
answers a different question, so each was decided by reading what it guards:

  terminal_states            YES -- a withheld capsule is finished; nothing follows it.
  joined_states              NO  -- it was never joined to a provider payload. That IS the
                                    holdout. Requiring joined_capsule_hash would demand proof
                                    of the very thing we deliberately did not do.
  provider_states            NO  -- no provider call carried this capsule, so there is no
                                    provider_response_id to require. (The native call has one,
                                    but that call did not carry the capsule.)
  terminal_expected          NO  -- there is no ProviderTerminalKind; the provider was never
                                    asked about this capsule.
  retryable_delivery_states  NO  -- retrying would DELIVER evidence the coin said to withhold,
                                    silently breaking the randomization it exists to create.

NOT YET REACHABLE. This commit adds the state and its edge; nothing PRODUCES it until the
holdout is wired at the boundary. That is deliberate and declared, not an oversight -- the
state machine is the precondition, and landing it separately keeps a proof-chain change
reviewable on its own.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.reasoning_runtime import DeliveryState as DS


def test_the_state_exists_and_is_distinct_from_failure_terminals() -> None:
    assert DS.WITHHELD_FOR_MEASUREMENT.value == "WITHHELD_FOR_MEASUREMENT"
    assert DS.WITHHELD_FOR_MEASUREMENT is not DS.CANCELLED
    assert DS.WITHHELD_FOR_MEASUREMENT is not DS.RESPONSE_DISCARDED


def test_only_a_compiled_capsule_may_be_withheld() -> None:
    """The holdout is decided AFTER compilation and BEFORE binding/dispatch.

    Withholding a capsule that had already been dispatched would be a lie: the bytes were
    sent. Withholding one that was never compiled is meaningless: there is nothing to withhold.
    """
    assert DS.WITHHELD_FOR_MEASUREMENT in rr._DELIVERY_TRANSITIONS[DS.COMPILED]
    for source, targets in rr._DELIVERY_TRANSITIONS.items():
        if source is not DS.COMPILED:
            assert DS.WITHHELD_FOR_MEASUREMENT not in targets, (
                f"{source.value} must not be able to reach WITHHELD_FOR_MEASUREMENT"
            )


def test_withheld_is_terminal_nothing_follows_it() -> None:
    assert DS.WITHHELD_FOR_MEASUREMENT not in rr._DELIVERY_TRANSITIONS
    assert DS.WITHHELD_FOR_MEASUREMENT not in rr._INITIAL_DELIVERY_TRANSITIONS


def test_the_initial_table_inherits_the_edge_rather_than_duplicating_it() -> None:
    """Step 1's composition must carry the new edge for free — no second hand-edit."""
    assert (
        rr._INITIAL_DELIVERY_TRANSITIONS[DS.COMPILED]
        == rr._DELIVERY_TRANSITIONS[DS.COMPILED]
    )
    assert DS.WITHHELD_FOR_MEASUREMENT in rr._INITIAL_DELIVERY_TRANSITIONS[DS.COMPILED]


def test_withheld_does_not_demand_proof_of_what_was_deliberately_not_done() -> None:
    """A withheld capsule has no join proof, no provider response, no provider terminal.

    Driven through the real DeliveryAttempt validator: constructing one must NOT raise for a
    missing provider_payload_hash / provider_response_id, because requiring them would make the
    holdout unrepresentable.
    """
    attempt = rr.DeliveryAttempt(
        evidence_ids=("GT-E-withheld",),
        capsule_hash="c" * 64,
        model_call_id="call-withheld",
        state=DS.WITHHELD_FOR_MEASUREMENT,
        observation_id="obs-withheld",
    )
    assert attempt.state is DS.WITHHELD_FOR_MEASUREMENT
    assert attempt.provider_response_id == ""
    assert attempt.joined_capsule_hash == ""


def test_withheld_is_not_retryable_because_retrying_would_break_randomization() -> None:
    """Pinned as a PROPERTY, not a preference.

    A retry would deliver the evidence the coin said to withhold. The holdout would silently
    become a delayed delivery and the propensity would be wrong -- an experiment that quietly
    stops being the experiment.
    """
    import inspect

    src = inspect.getsource(rr)
    marker = "retryable_delivery_states = {"
    start = src.index(marker)
    block = src[start:src.index("}", start)]
    assert "WITHHELD_FOR_MEASUREMENT" not in block
