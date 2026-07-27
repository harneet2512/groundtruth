"""`iteration` must advance on the CANONICAL path, not only the legacy one.

THE MEASUREMENT DEFECT. `_runtime_ledger_record` stamps
`"iteration": globals().get("_action_count", 0)`, and BOTH increments of `_action_count` live
inside `_augment_output_legacy`. When the canonical runtime is attached, `_augment_output`
routes to the observer and that counter is never touched -- so every canonical row in a
healthy run is stamped `iteration=0`, for the entire trajectory, by construction.

WHAT THAT COST. Run 30283834926 showed hydra-3005 with 57 `canonical_runtime.compilation`
rows, ALL at iteration 0, and I reported "the oracle bursts at step 0 and never cycles".
That was wrong. hydra ran ~56 agent steps and produced 57 compiles -- ONE PER OBSERVATION.
The oracle was cycling across the whole trajectory; the counter was dead.

Worse, the artifact looked self-consistent. In the PRE-fix runs the observer died early and
`dark_fallback` handed control back to legacy, which DOES increment -- so dark_fallback rows
showed iterations 1,2,3... while canonical compile rows sat frozen at 0. That reads exactly
like "burst at 0, then die", and it is not what happened.

THE FIX. When the canonical runtime is attached, stamp `iteration` from
`attempt_runtime.work_state.sequence`, which advances on every appended event. Legacy runs
keep `_action_count` byte-identically.

DO NOT "fix" this by incrementing `_action_count` from the observer. Two writers on one
global, with only one of them owning the observation loop, is how it broke.
"""

from __future__ import annotations

import types

from artifact_deepswe import gt_mini_patch as seam


class _WS:
    def __init__(self, seq):
        self.sequence = seq


class _RT:
    def __init__(self, seq):
        self.work_state = _WS(seq)


def _attach(seq):
    return types.SimpleNamespace(attempt_runtime=_RT(seq))


def _rows(monkeypatch, attachment, action_count=0):
    captured: list[dict] = []
    monkeypatch.setattr(seam, "_ledger_line_direct", lambda row: captured.append(row))
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", attachment, raising=False)
    monkeypatch.setattr(seam, "_action_count", action_count, raising=False)
    seam._runtime_ledger_record(kind="canonical_runtime.compilation",
                                outcome="suppressed_internal_only", reason="probe", chars=0)
    return captured


def test_canonical_rows_carry_the_canonical_sequence(monkeypatch):
    """THE FIX. A canonical row must reflect where in the trajectory it happened."""
    rows = _rows(monkeypatch, _attach(41), action_count=0)
    assert rows, "no ledger row captured"
    assert rows[0]["iteration"] == 41, (
        f"canonical row stamped iteration={rows[0]['iteration']} while the runtime was at "
        "work_state.sequence=41 -- every canonical run is unreadable this way"
    )


def test_the_stamp_advances_across_observations(monkeypatch):
    """The whole point: distinct observations must be distinguishable."""
    seen = [_rows(monkeypatch, _attach(n))[0]["iteration"] for n in (1, 7, 23)]
    assert seen == [1, 7, 23], f"stamp did not track the sequence: {seen}"


def test_legacy_runs_are_byte_identical(monkeypatch):
    """NEAR-NEGATIVE / ANTI-REGRESSION. With no attachment the legacy counter still owns
    the stamp -- this change must not alter any GT-off or legacy-path artifact."""
    rows = _rows(monkeypatch, None, action_count=9)
    assert rows[0]["iteration"] == 9


def test_a_broken_attachment_falls_back_instead_of_raising(monkeypatch):
    """Correct-or-quiet: telemetry must never raise into the agent. An attachment missing
    the runtime or work_state falls back to the legacy counter."""
    for bad in (types.SimpleNamespace(),
                types.SimpleNamespace(attempt_runtime=None),
                types.SimpleNamespace(attempt_runtime=types.SimpleNamespace())):
        rows = _rows(monkeypatch, bad, action_count=4)
        assert rows[0]["iteration"] == 4, f"bad attachment {bad!r} did not fall back"


def test_the_two_counters_are_not_merged(monkeypatch):
    """ANTI-REGRESSION on the SHAPE of the fix. `_action_count` must NOT be incremented from
    the canonical path: two writers on one global, only one of which owns the observation
    loop, is exactly how this broke."""
    import inspect

    src = inspect.getsource(seam._runtime_ledger_record)
    assert "_action_count += 1" not in src, (
        "the ledger writer now mutates the legacy counter -- that re-creates the two-writer "
        "bug this fix exists to remove"
    )
