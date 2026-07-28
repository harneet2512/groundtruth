"""C32 — provenance sanitization destroys the def_partition sidecar, so the class cannot attest.

`_ss_rebind_sanitized_producer_inputs` re-binds a sanitized Gateway envelope's ProducerInputs to
the re-derived candidate id, keeping only the caller evidence that survived sanitization. It then
fails closed:

    if not caller_rows:
        return None, "caller_rows"

That guard is right for an EDIT fact, whose evidence IS its callers: if sanitization removed them
all, attesting would fabricate a caller the delivery no longer carries. It is wrong for a SEARCH
fact. `_def_partition_inputs` (gateway.py:2083-2092) builds its sidecar with `caller_rows=()` BY
CONSTRUCTION — its evidence is `definition_rows`. So the guard cannot distinguish "the callers
were sanitized away" from "there were never any callers", and it drops a sidecar that lost
nothing.

CONSEQUENCE, which is why this is not cosmetic: the envelope then ships with
`producer_inputs=None`, `build_gateway_attestation` raises "producer inputs missing or wrong
type", the persist wrapper swallows it into a measurement_failed row, and def_partition's
`correct_info` is never True on any delivery that needed sanitization. Four evidence types ride
this sidecar — def_ref_partition, name_fold, wrong_surface, body_concept — i.e. the whole class.

THE FIX IS THE NARROWEST ONE THAT PRESERVES THE GUARD'S PURPOSE: fail closed only when the
sidecar HAD caller rows and none survived. A sidecar that never carried callers has no caller to
fabricate, so there is nothing for the guard to protect.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — restore the unconditional `if not caller_rows` guard:
       `test_a_callerless_sidecar_survives_sanitization` goes RED and def_partition is dark again.
  M2 — drop the guard entirely (never fail closed):
       `test_an_edit_sidecar_that_loses_every_caller_still_fails_closed` goes RED, and a
       caller-bearing fact would attest callers the delivery no longer carries.
"""

from __future__ import annotations

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    DefinitionRow,
    ProducerInputs,
)


_DEF_ROW = DefinitionRow(
    identity="parse_config",
    file="src/config/loader.py",
    line=42,
    kind="function",
    definition_id=7,
    confidence=1.0,
    resolution_method="graph",
)


def _caller(file: str, line: int) -> CallerEvidenceRow:
    return CallerEvidenceRow(
        identity="call_site",
        file=file,
        line=line,
        confidence=1.0,
        resolution_method="graph",
        source_state=None,
        edge_id=3,
        definition_id=7,
    )


def _inputs(*, caller_rows=(), definition_rows=()) -> ProducerInputs:
    return ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type="def_ref_partition",
        candidate_id="stale-precheck-key",
        before_state=None,
        after_state=None,
        caller_rows=tuple(caller_rows),
        graph_revision="graph-rev",
        definition_rows=tuple(definition_rows),
    )


def test_the_probe_can_produce_a_non_zero() -> None:
    """CALIBRATION. A rebind that always returned None would make every assertion unreadable."""
    inputs = _inputs(caller_rows=(_caller("src/a.py", 10),))
    rebound, gap = seam._ss_rebind_sanitized_producer_inputs(
        inputs, (("src/a.py", 10),), "fresh-key"
    )
    assert gap is None
    assert rebound is not None
    assert rebound.candidate_id == "fresh-key"


def test_a_callerless_sidecar_survives_sanitization() -> None:
    """M1. The def_partition shape: no callers by construction, evidence is definition_rows.

    Its candidate_id must still be re-bound to the sanitized key, or the attestation raises
    'producer inputs candidate mismatch' — the very defect this rebind exists to prevent.
    """
    inputs = _inputs(definition_rows=(_DEF_ROW,))
    assert inputs.caller_rows == ()

    rebound, gap = seam._ss_rebind_sanitized_producer_inputs(
        inputs, (("src/config/loader.py", 42),), "fresh-key"
    )

    assert gap is None, "a sidecar that never carried callers has lost nothing"
    assert rebound is not None
    assert rebound.candidate_id == "fresh-key"
    assert rebound.definition_rows == (_DEF_ROW,), "its actual evidence must be preserved"


def test_an_edit_sidecar_that_loses_every_caller_still_fails_closed() -> None:
    """M2. The guard's real purpose, preserved: never attest a caller the delivery dropped."""
    inputs = _inputs(caller_rows=(_caller("src/leaked.py", 99),))

    rebound, gap = seam._ss_rebind_sanitized_producer_inputs(
        inputs, (("src/kept.py", 1),), "fresh-key"
    )

    assert rebound is None
    assert gap == "caller_rows"


def test_a_partially_sanitized_edit_sidecar_keeps_only_survivors() -> None:
    inputs = _inputs(
        caller_rows=(_caller("src/kept.py", 1), _caller("src/leaked.py", 99))
    )

    rebound, gap = seam._ss_rebind_sanitized_producer_inputs(
        inputs, (("src/kept.py", 1),), "fresh-key"
    )

    assert gap is None
    assert rebound is not None
    assert [row.file for row in rebound.caller_rows] == ["src/kept.py"]
