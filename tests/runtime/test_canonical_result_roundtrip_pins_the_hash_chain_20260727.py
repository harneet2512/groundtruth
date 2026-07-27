"""Every `CanonicalResult` field must survive a JSON round-trip, or the hash chain breaks.

THE TRAP THIS CLOSES (hit for real on 2026-07-27).

Canonical events are hash-chained: `content_hash` covers the event's content, including its
`CanonicalResult`. But `CanonicalEvent.from_json` rebuilds that result from a HAND-MAINTAINED field
list. Adding `viewed_symbols` to the dataclass without adding it to that list meant the event
HASHED with the field and REHYDRATED without it, so the recomputed hash disagreed with the stored
one and the chain died with:

    EventIntegrityError: event content hash/tamper mismatch at sequence 2

The failure surfaced nowhere near its cause. `observe_action_result` swallows observer faults by
design ("native result must survive"), so the visible symptom was that the gateway produced NOTHING
and no canonical events were committed -- which reads exactly like "this feature never fires."
Every unit test still passed, because none of them re-verified the chain; only two end-to-end
drivers caught it.

WHY A GENERIC TEST. This is the same defect class as every other bug found that day: TWO FORMULAS
THAT MUST AGREE BY HAND. Pinning `viewed_symbols` specifically would close one instance and leave
the trap armed for the next field. Deriving the field list from `dataclasses.fields` means the test
fails automatically for ANY field added to the dataclass but not to the deserializer -- the person
who adds it learns immediately, here, instead of debugging a silent observer fault later.
"""

from __future__ import annotations

import dataclasses

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-rt", graph="graph-rt", lsp="lsp-rt", runtime_evidence="rt-rt",
)

# One distinctive non-default value per field type, so a dropped field cannot coincidentally
# equal its default and pass.
_PROBES: dict[str, object] = {
    "status": "success",
    "exit_code": 7,
    "changed": True,
    "hit_count": 3,
    "files_hit": ("a/one.py", "b/two.py"),
    "changed_files": ("c/three.py",),
    "viewed_symbols": ("alpha", "beta"),
    "resolved_symbols": ("gamma",),
    "failure_fingerprint": "fp-distinctive",
    "signature_before": "def f(a)",
    "signature_after": "def f(a, b)",
}


def _probe_result() -> rr.CanonicalResult:
    return rr.CanonicalResult(**_PROBES)  # type: ignore[arg-type]


def test_the_probe_covers_every_field_of_the_dataclass():
    """POSITIVE CONTROL on the test itself. If a field is added to `CanonicalResult` and not to
    `_PROBES`, this fails first -- otherwise the round-trip below would silently stop covering
    it and the trap would be re-armed by the very test meant to prevent it."""
    declared = {f.name for f in dataclasses.fields(rr.CanonicalResult)}
    assert declared == set(_PROBES), (
        f"probe/field drift: missing={declared - set(_PROBES)}, "
        f"extra={set(_PROBES) - declared}"
    )


def _round_trip(event):
    # `canonical_json` is the serialized form the journal stores and re-verifies; `from_json`
    # is its inverse. The pair IS the hash-chain contract under test.
    return rr.CanonicalEvent.from_json(event.canonical_json())


def _event_with(result: rr.CanonicalResult):
    from groundtruth.runtime.adapters import miniswe
    from groundtruth.runtime.gateway import ToolEvent

    action = rr.CanonicalAction(
        action_id="a-rt",
        operation=rr.ActionOperation.VIEW_SOURCE,
        tool_family="shell",
        tool_name="mini-swe",
        structured_operation="view",
        subject="src/pkg/mod.py",
        query="",
        targets=("src/pkg/mod.py",),
        raw_command="cat src/pkg/mod.py",
    )
    proposal = miniswe.canonicalize_action_proposal(
        action,
        event_id="e-rt-proposal",
        attempt_id="attempt-rt",
        sequence=1,
        model_turn_id="call-rt",
        observation_id="obs-rt",
        revision=REVISION,
        previous_event_hash="",
    )
    return miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="view", carrier_kind="view", command="cat src/pkg/mod.py",
            output="def alpha(): ...", exit_status=0,
            semantic_events=(), semantics_authoritative=True,
        ),
        proposal=proposal,
        result=result,
        event_id="e-rt-result",
        sequence=2,
        observation_id="obs-rt",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )


@pytest.mark.parametrize("field_name", sorted(_PROBES))
def test_each_field_survives_the_round_trip(field_name):
    """THE GUARD. A field that serializes but does not rehydrate changes the recomputed hash."""
    event = _event_with(_probe_result())
    restored = _round_trip(event)
    assert restored.result is not None
    assert getattr(restored.result, field_name) == _PROBES[field_name], (
        f"`{field_name}` did not survive from_json -- it is missing from the hand-maintained "
        "field list in CanonicalEvent.from_json, and the event hash chain will fail with "
        "EventIntegrityError far from this seam"
    )


def test_the_content_hash_is_stable_across_the_round_trip():
    """THE ACTUAL INVARIANT, stated directly rather than through its symptoms. This is the
    assertion whose violation produced `event content hash/tamper mismatch`."""
    event = _event_with(_probe_result())
    assert _round_trip(event).content_hash == event.content_hash, (
        "the rehydrated event hashes differently from the original -- the chain is broken"
    )
