r"""C2 — the compilation row must say WHICH lifecycle the stored evidence is in.

THE GAP THIS CLOSES. The fingerprint seen on every canonical run is
`evidence_store=1, coalition_size=0, held_evidence=0`. Two investigations returned OPPOSITE
readings of it, and adjudicating them against `reasoning_runtime.py` showed both were partly right:

  * `held_evidence` counts ONLY records the runtime transitioned INTO, or found already sitting in,
    HELD *in persistent storage* -- `:6434-6452` requires `current.lifecycle is READY`. So a
    relevance-gate HELD record that DID reach READY does increment it, and `held=0` genuinely
    EXCLUDES the relevance gate for that case.
  * But `:6321` promotes PENDING -> READY only when every contract `ready_predicate` is in the
    seam's satisfied set, while `:6330` appends the record to `ready_records` REGARDLESS. So a
    still-PENDING record is evaluated, fails the `is READY` clause at `:6436`, and is silently
    absent from `held_ids`. `held=0` does NOT exclude that.

So the same three numbers are produced by two OPPOSITE diagnoses with opposite fixes:
  (a) the record never reached READY   -> a readiness-predicate/contract bug
  (b) the stored lifecycle is terminal -> a freshness/consumption bug
and nothing on the row could tell them apart. This histogram is that discriminator.

WHY THE STORE AND NOT `ready_records`: `evidence_store` and `held_evidence` are both computed from
the store, so reading the same source keeps all three numbers commensurable BY CONSTRUCTION rather
than by hand-sync -- and two numbers that must agree by hand is the defect class this codebase keeps
reproducing.
"""

from __future__ import annotations

import inspect

from artifact_deepswe import gt_mini_patch as seam


class _Rec:
    def __init__(self, lifecycle) -> None:
        self.lifecycle = lifecycle


class _Life:
    """Stands in for the EvidenceLifecycle enum member: the helper reads only `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Runtime:
    def __init__(self, records) -> None:
        self._evidence = {f"ev-{i}": r for i, r in enumerate(records)}


def test_positive_control_the_histogram_counts_what_is_there():
    """Run FIRST. Every empty/zero assertion below is unreadable until the helper is shown able
    to produce a populated answer."""
    rt = _Runtime([_Rec(_Life("READY")), _Rec(_Life("READY")), _Rec(_Life("PENDING"))])
    assert seam._evidence_lifecycle_histogram(rt) == {"READY": 2, "PENDING": 1}


def test_it_separates_C2s_candidate_a_never_reached_READY():
    """(a) The record never got promoted. PENDING>0 is the proof, and it is invisible in
    evidence_store / held_evidence / coalition_size, all of which read exactly as they do for (b)."""
    rt = _Runtime([_Rec(_Life("PENDING"))])
    hist = seam._evidence_lifecycle_histogram(rt)
    assert hist == {"PENDING": 1}
    assert "READY" not in hist


def test_it_separates_C2s_candidate_b_terminal_or_consumed():
    """(b) `:6415-6433` transitions terminal records WITHOUT appending to held_ids, so these
    produce held_evidence=0 too. Only the lifecycle name distinguishes them from (a)."""
    for terminal in ("INVALIDATED", "EXPIRED", "RELEASED", "DELIVERED", "SATISFIED", "SUPERSEDED"):
        rt = _Runtime([_Rec(_Life(terminal))])
        assert seam._evidence_lifecycle_histogram(rt) == {terminal: 1}


def test_the_three_C2_shapes_are_now_mutually_distinguishable():
    """THE POINT OF THE WHOLE FIELD. All three produce evidence_store=1, coalition_size=0,
    held_evidence=0 on today's row. They must not produce the same histogram."""
    a = seam._evidence_lifecycle_histogram(_Runtime([_Rec(_Life("PENDING"))]))
    b = seam._evidence_lifecycle_histogram(_Runtime([_Rec(_Life("INVALIDATED"))]))
    c = seam._evidence_lifecycle_histogram(_Runtime([_Rec(_Life("HELD"))]))
    assert a != b and b != c and a != c, f"shapes collide: {a} {b} {c}"


def test_it_is_commensurable_with_evidence_store():
    """The histogram must total to what `evidence_store` reports, or the two numbers are about
    different populations and differencing them is the cross-namespace trap again."""
    records = [_Rec(_Life("READY")), _Rec(_Life("PENDING")), _Rec(_Life("DELIVERED"))]
    rt = _Runtime(records)
    hist = seam._evidence_lifecycle_histogram(rt)
    assert sum(hist.values()) == len(rt._evidence)


def test_correct_or_quiet_on_a_broken_runtime():
    """A telemetry fault must leave the key ABSENT (reads NOT-EVALUABLE) rather than emit a false
    zero, and must never raise into the agent's turn."""
    class _Exploding:
        @property
        def _evidence(self):
            raise RuntimeError("boom")

    assert seam._evidence_lifecycle_histogram(_Exploding()) == {}
    assert seam._evidence_lifecycle_histogram(object()) == {}
    assert seam._evidence_lifecycle_histogram(_Runtime([])) == {}
    # A record whose lifecycle has no usable name is SKIPPED, never counted under a made-up key.
    assert seam._evidence_lifecycle_histogram(_Runtime([_Rec(None)])) == {}


def test_all_four_row_writers_carry_the_field():
    """Both compilation branches (success + failure), the step-0 rows, AND the P1-3
    precommit staging row (2026-07-29) must carry it. A field present on only some
    branches cannot be differenced across the branch boundary, which is exactly the
    question C2 asks: what changed between a quiet turn and a staged one."""
    src = inspect.getsource(seam)
    assert src.count('"evidence_lifecycles"') == 4, (
        f'expected 4 writers, found {src.count(chr(34) + "evidence_lifecycles" + chr(34))}'
    )
