"""CAP participation must survive an OFF-BOUNDARY delivery, or the record is self-selecting.

THE BIAS. `build_caller_contract_control_participation` re-derives the producer-input bytes and
compares their hash to the one the attestation recorded. It reconstructs them with::

    actual_event=attestation.decision.required_event      # the CONTRACTED event

but the bytes were originally built with the OBSERVED event. `DecisionBinding` persists only
`open_event` and `required_event` -- never `actual_event` -- so when a delivery lands off its
contracted boundary the two differ, the hashes disagree, and the function returns `()`. The
participation record is dropped SILENTLY.

WHY THAT IS CORROSIVE. It means a CAP participation record can only ever exist for an ON-TIME
delivery. Any statistic computed over those records is conditioned on the outcome it is meant to
measure: late deliveries do not show up as late, they vanish. That is the same shape as an
on-time check that cannot fail -- a measurement that confirms itself.

THE FIX. Production's SOLE call site builds the attestation with
``actual_event=actual_event, open_event=actual_event`` -- the observed event is carried in
`open_event`. Reconstructing from `decision.open_event` therefore reproduces the original bytes
EXACTLY, on-boundary and off-boundary alike, with no schema change and no new field.

Note every pre-existing test in this family passes ``actual_event="edit_result"``, which IS
`required_event("caller_break")`. They are all on-boundary, which is precisely why none of them
caught this.
"""

from __future__ import annotations

import hashlib

import pytest

from groundtruth.runtime.fact_registry import required_event
from groundtruth.runtime.gateway_attestation_factory import (
    build_caller_contract_control_participation,
    build_gateway_attestation,
)
from groundtruth.runtime.adapters.miniswe import render_envelope

from tests.runtime.test_gateway_caller_contract_family_20260715 import _envelope


CONTRACTED = required_event("caller_break")          # "edit_result"
OFF_BOUNDARY = "file_view"                            # a real EVENTS member, not the contracted one


def _attested(actual_event: str):
    env = _envelope()
    shipped = render_envelope(env, native=True).encode()
    attestation, _artifacts = build_gateway_attestation(
        env,
        delivery_seal=hashlib.sha256(shipped).hexdigest()[:16],
        shipped_bytes=shipped,
        actual_event=actual_event,
        open_event=actual_event,          # exactly what the sole production call site does
    )
    return env, attestation, shipped


def _participation(actual_event: str):
    env, attestation, shipped = _attested(actual_event)
    return build_caller_contract_control_participation(
        env,
        attestation=attestation,
        shipped_bytes=shipped,
        native=True,
        enabled_features={"GT_CONTRACT_NATIVE"},
        iteration=3,
    )


def test_premise_the_two_events_actually_differ():
    """POSITIVE CONTROL ON THE PREMISE. If OFF_BOUNDARY ever equals the contracted event this
    whole file is vacuous -- it would prove only that on-boundary works, which is already
    covered."""
    assert OFF_BOUNDARY != CONTRACTED


def test_on_boundary_participation_is_recorded():
    """POSITIVE CONTROL ON THE INSTRUMENT. The on-time case must work, or the off-boundary
    assertion below cannot distinguish 'the bias' from 'participation is broken outright'."""
    rows = _participation(CONTRACTED)
    assert rows, "on-boundary participation is empty -- the instrument is dead, not biased"
    assert {r.feature_id for r in rows} == {"GT_CONTRACT_NATIVE"}


def test_off_boundary_participation_is_ALSO_recorded():
    """THE FIX. A late delivery is still a delivery; its participation must be observable, or
    every statistic over these records is conditioned on being on time."""
    rows = _participation(OFF_BOUNDARY)
    assert rows, (
        "an off-boundary delivery produced NO participation record -- the reconstruction is "
        "still substituting required_event, so late deliveries silently vanish and the "
        "resulting statistics are self-selecting"
    )
    assert {r.feature_id for r in rows} == {"GT_CONTRACT_NATIVE"}


def test_both_paths_agree_on_the_delivered_identity():
    """The bytes are identical either way -- only the recorded event differs. If the seal or
    candidate identity changed with the boundary, the fix would be masking a real mismatch
    rather than removing a false one."""
    on_rows = _participation(CONTRACTED)
    off_rows = _participation(OFF_BOUNDARY)
    assert {r.candidate_sha256_16 for r in on_rows} == {
        r.candidate_sha256_16 for r in off_rows
    }
    assert {r.candidate_id for r in on_rows} == {r.candidate_id for r in off_rows}


def test_bytes_that_are_not_the_canonical_render_are_rejected():
    """ADDED BECAUSE A MUTATION SURVIVED (the shipped-bytes exactness check).

    My first anti-weakening test appended junk to `shipped_bytes`, which ALSO breaks the
    delivery-seal comparison -- so a different guard rejected it and this check was never
    exercised. Deleting the exactness check left every test green.

    Isolate it: attest over bytes that are not the canonical render, so the SEAL matches them
    and only `shipped_bytes not in (expected, b"\\n" + expected)` can refuse. Participation must
    still be refused, or a record could attest bytes the renderer would never produce.
    """
    env = _envelope()
    non_canonical = render_envelope(env, native=True).encode() + b"  # not the render"
    attestation, _ = build_gateway_attestation(
        env,
        delivery_seal=hashlib.sha256(non_canonical).hexdigest()[:16],
        shipped_bytes=non_canonical,
        actual_event=CONTRACTED,
        open_event=CONTRACTED,
    )
    assert build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=non_canonical, native=True,
        enabled_features={"GT_CONTRACT_NATIVE"}, iteration=3,
    ) == (), "bytes that are not the canonical render were accepted"


def test_tampered_producer_inputs_are_rejected():
    """ADDED BECAUSE A MUTATION SURVIVED (the producer-input hash check).

    Nothing in this file previously altered `producer_inputs`, so deleting the comparison
    between the recorded artifact sha and the re-derived input bytes changed no test outcome.
    That comparison is the whole reason the reconstruction has to be faithful -- loosening
    WHICH event it reconstructs with must not loosen WHETHER the result is checked.
    """
    import dataclasses

    env, attestation, shipped = _attested(CONTRACTED)
    caller_rows = env.producer_inputs.caller_rows
    tampered = dataclasses.replace(
        env,
        producer_inputs=dataclasses.replace(
            env.producer_inputs,
            caller_rows=(
                dataclasses.replace(caller_rows[0], line=caller_rows[0].line + 41),
            ) + tuple(caller_rows[1:]),
        ),
    )
    assert build_caller_contract_control_participation(
        tampered, attestation=attestation, shipped_bytes=shipped, native=True,
        enabled_features={"GT_CONTRACT_NATIVE"}, iteration=3,
    ) == (), "producer inputs were altered after attestation and participation still recorded"


def test_a_genuine_byte_mismatch_is_still_rejected():
    """ANTI-WEAKENING, and the line this fix must not cross. Loosening the reconstruction must
    not loosen the HASH CHECK: bytes that were never the ones attested must still be refused,
    or the participation record stops proving anything at all."""
    env, attestation, shipped = _attested(CONTRACTED)
    assert build_caller_contract_control_participation(
        env,
        attestation=attestation,
        shipped_bytes=shipped + b"tampered",
        native=True,
        enabled_features={"GT_CONTRACT_NATIVE"},
        iteration=3,
    ) == ()
