"""An observer fault must name WHICH invariant tripped, not just the exception class.

WHY.  On run 30246661710 the oracle evaluated 45 times at iteration 0, then emitted exactly
one row -- `canonical_runtime  observe_failed:StateIntegrityError` -- and from iteration 1
onward the ledger is nothing but `dark_fallback`. That single error is what isolates the
canonical observer and stops the oracle from EVER cycling again: it bursts at step 0, dies on
the first real observation, and the rest of the trajectory runs with no timing authority.

Fixing it requires knowing which invariant raised. `reduce_event` and the reasoning-graph
reducer raise `StateIntegrityError` from ~15 distinct places, and the six reachable from
`observe_action_result` are materially different bugs:

    attempt identity mismatch
    event sequence gap: expected N, got M
    repository revision mismatch before reduction
    repository content revision did not advance for a mutation outcome
    canonical reasoning event sequence gap
    reasoning event previous hash mismatch

`reason=f"observe_failed:{type(exc).__name__}"` collapses all of them to one string, so the
artifact cannot distinguish a sequencing bug from a repository-revision bug from a hash-chain
bug. That is why the run's own ledger cannot explain why its oracle went dark.

SAFETY.  These messages are fixed strings plus small integers -- no repository content, no
paths, no secrets. The row stays host-side only (`suppressed_internal_only`, `chars=0`, never
model-facing) and is length-capped so a future message that does embed content cannot bloat
the ledger or leak a payload into it.
"""

from __future__ import annotations

import inspect

from artifact_deepswe import gt_mini_patch as seam


def _observe_fault_block() -> str:
    """The except-handler that records the observer fault.

    Sliced structurally and ENDING at the next method definition. An earlier version ran to
    `idx + 700`, which reached into `observe_action_exception` and matched ITS `str(exc)` --
    so the assertion below passed while the fault row still recorded only the class name.
    A test whose window extends past the code under test is vacuous.
    """
    src = inspect.getsource(seam)
    # Anchor on a STABLE token. An earlier version anchored on `reason=f"observe_failed:`
    # and broke the moment that f-string was split across lines -- a helper that depends on
    # incidental formatting is a brittle harness, not a test.
    idx = src.index('kind="canonical_runtime",')
    start = src.rindex("except Exception", 0, idx)
    end = src.index("\n    def ", idx)
    return src[start:end]


def test_fault_row_carries_the_exception_message():
    """The whole point: WHICH invariant, not merely 'a StateIntegrityError happened'."""
    block = _observe_fault_block()
    assert "type(exc).__name__" in block, "the class name is still useful -- keep it"
    assert "_fault_detail" in block or "str(exc)" in block, (
        "the fault row records only the exception CLASS. Six materially different "
        "invariants collapse to 'StateIntegrityError', so the ledger cannot say why the "
        "observer went dark -- which is the one thing needed to fix it"
    )


def test_the_detail_is_length_capped():
    """A future invariant message could embed content; the ledger must not become a
    payload channel or grow unboundedly."""
    block = _observe_fault_block()
    assert any(tok in block for tok in ("[:200]", "[:256]", "[:160]", "[:120]")), (
        "no length cap on the recorded detail"
    )


def test_row_stays_host_side_and_ships_no_bytes():
    """ANTI-REGRESSION. Diagnostics must never become model-facing.

    Scoped to THIS record call. An earlier version asserted `"chars=0" in block`, which the
    surrounding slice satisfied from an unrelated call -- a mutation setting
    `chars=len(_fault_detail)` on this very row SURVIVED it. An assertion that passes while
    the thing it guards is broken is worse than no assertion.
    """
    block = _observe_fault_block()
    start = block.index('kind="canonical_runtime",')
    call = block[start : block.index(")", block.index("chars=", start))]
    assert "chars=0" in call, (
        f"the fault row now ships bytes -- diagnostics became model-facing: {call!r}"
    )
    assert '"suppressed_internal_only"' in call


def test_recording_the_detail_cannot_raise_into_the_agent():
    """Correct-or-quiet: the fault recorder itself must stay guarded."""
    block = _observe_fault_block()
    assert "except Exception" in block


def test_the_six_reachable_invariants_still_exist_upstream():
    """POSITIVE CONTROL. If these messages were renamed, the detail this test demands would
    still be recorded but would no longer match anything known -- and a future reader would
    be back to guessing. Pin the strings that make the detail meaningful."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import groundtruth.runtime.reasoning_runtime as rr

    src = inspect.getsource(rr)
    for phrase in (
        "attempt identity mismatch",
        "event sequence gap",
        "repository revision mismatch before reduction",
        "repository content revision did not advance for a mutation outcome",
    ):
        assert phrase in src, f"invariant message vanished: {phrase!r}"
