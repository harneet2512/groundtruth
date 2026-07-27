"""`runtime_evidence` must not change on every observation.

THE BUG (root cause of "the oracle never composes a capsule"). The revision vector's
`runtime_evidence` dimension is computed by TWO DIFFERENT formulas, and the per-observation
one embeds the monotonically increasing sequence::

    initial         sha256(f"{attempt_seed}:runtime:0")                      (gt_mini_patch)
    per-observation sha256(f"{attempt_id}:{sequence}:{repository}:{graph}")  (gt_mini_patch)

So `runtime_evidence` changes on EVERY observation, by construction, and the two formulas can
never agree on any input.

WHY THAT IS FATAL. `prepare_next_inference` runs `invalidate_stale_evidence` first. Any record
whose stamped dimension differs from the current revision becomes INVALIDATED, which is
terminal -- the composer suppresses it as NOT_READY. `_REVISION_DEPENDENCY_DIMENSION` maps
`issue`, `patch_rev`, `edit_rev` and `episode_state` onto `runtime_evidence`, so obligations,
syntax_result, covering_red, submit_refusal and recovery ALL inherit this churn.

Consequence: every such record's eligibility window is exactly ONE observation cycle -- and
the step-0 obligations record has NO valid window at all, because the initial and
per-observation formulas differ. `obligations` is the only standing carrier of
BEHAVIORAL_CONTRACT, the required role of PATCH_CONSTRUCTION and SOURCE_UNDERSTANDING.
Measured across two full runs: `unresolved_roles` is BEHAVIORAL_CONTRACT on 70 of 90 compile
attempts, with `coalition_size=0` on all 90.

The `held_evidence=0, evidence_store=1` signature seen throughout those runs is the
INVALIDATED fingerprint -- invalidated records are excluded from `held_evidence_ids` -- which
is exactly why it was misread as "no evidence existed".

THE FIX. One function, used by both call sites, that does NOT depend on the sequence.
`runtime_evidence` must digest actual runtime-evidence STATE, so it changes when that state
changes and not merely because time passed.

SCOPE NOTE, deliberately not fixed here: `issue` (the immutable issue text) still maps onto a
MUTABLE dimension, so an obligations record will still invalidate when the repository or graph
digest moves. That is a separate semantic decision about
`_REVISION_DEPENDENCY_DIMENSION` and is tracked separately -- do not silently widen this fix
into that one.
"""

from __future__ import annotations

import inspect

from artifact_deepswe import gt_mini_patch as seam


def _seam_src() -> str:
    return inspect.getsource(seam)


def test_the_per_observation_formula_does_not_embed_the_sequence():
    """THE FIX. A pure passage of observations must not age evidence out."""
    # Structural, not a source-wide string scan: the helper's own docstring quotes the old
    # formula to explain the defect, and a naive `"{sequence}" not in src` fails on that
    # documentation. Assert the HELPER cannot depend on the sequence -- it is not a parameter
    # and is not referenced in its body.
    helper = getattr(seam, "_runtime_evidence_digest", None)
    assert helper is not None, "helper missing"
    params = set(inspect.signature(helper).parameters)
    assert "sequence" not in params, f"the digest still takes a sequence: {params}"
    body = inspect.getsource(helper)
    body = body[body.index('"""', body.index('"""') + 3) + 3:]   # strip the docstring
    assert "sequence" not in body, (
        "runtime_evidence still embeds the event sequence -- every record is INVALIDATED one "
        "cycle after production, so no coalition can ever complete"
    )


def test_both_call_sites_use_one_shared_function():
    """The initial stamp and the per-observation stamp must be the SAME function. Two
    formulas that must agree by hand is the defect, not the symptom."""
    src = _seam_src()
    assert "_runtime_evidence_digest" in src, (
        "no shared digest helper -- the initial and per-observation formulas can still drift"
    )
    assert src.count("_runtime_evidence_digest(") >= 3, (
        "the helper exists but both call sites do not use it (expect 1 def + 2 uses)"
    )


def test_the_helper_is_stable_across_observations():
    """BEHAVIOURAL. Same inputs -> same digest, regardless of how many observations passed."""
    helper = getattr(seam, "_runtime_evidence_digest", None)
    assert helper is not None, "helper missing"
    a = helper(attempt_id="A", repository="r1", graph="g1")
    b = helper(attempt_id="A", repository="r1", graph="g1")
    assert a == b, "digest is not deterministic for identical inputs"


def test_the_helper_still_moves_when_real_state_moves():
    """NEAR-NEGATIVE, and the anti-weakening line. Freshness must still MEAN something: a
    digest that never changes would make every record eternally fresh, which is the opposite
    failure and would let stale evidence ship."""
    helper = seam._runtime_evidence_digest
    base = helper(attempt_id="A", repository="r1", graph="g1")
    assert helper(attempt_id="A", repository="r2", graph="g1") != base, (
        "repository content moved and the digest did not -- stale evidence would stay fresh"
    )
    assert helper(attempt_id="A", repository="r1", graph="g2") != base, (
        "graph moved and the digest did not"
    )
    assert helper(attempt_id="B", repository="r1", graph="g1") != base, (
        "a different attempt must not share a runtime-evidence digest"
    )


def test_the_invalidation_machinery_is_untouched():
    """POSITIVE CONTROL on the PREMISE. This fix is about what the digest DEPENDS on, not
    about disabling freshness. `invalidate_stale_evidence` and the dependency map must
    survive intact -- otherwise this file is quietly removing a real guard."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import groundtruth.runtime.reasoning_runtime as rr

    rsrc = inspect.getsource(rr)
    assert "def invalidate_stale_evidence" in rsrc
    assert '"issue": "runtime_evidence"' in rsrc, (
        "the dependency mapping changed; re-derive this fix before trusting it"
    )


def test_the_call_sites_pass_real_state_not_the_sequence():
    """ANTI-REGRESSION found by a SURVIVING mutation.

    The tests above pin that the HELPER cannot take a sequence. They did not pin what the
    CALL SITES pass -- so a mutation substituting `attempt_id=str(sequence)` reintroduced the
    entire defect and passed everything. The argument list is part of the fix.
    """
    src = inspect.getsource(seam)
    idx = src.index("runtime_evidence = _runtime_evidence_digest(")
    call = src[idx : src.index(")", src.index("graph=", idx))]
    assert "attempt_id=self.attempt_runtime.attempt_id" in call, (
        f"the observation site no longer passes the real attempt id: {call!r}"
    )
    assert "sequence" not in call, (
        f"the sequence is being smuggled into the digest through an argument: {call!r}"
    )
    # And the initial site must pass the SAME attempt identity, or the two digests can never
    # agree and the step-0 record stays dead -- which is the bug this fix exists to remove.
    init = src.index("runtime_revision = _runtime_evidence_digest(")
    icall = src[init : src.index(")", src.index("graph=", init))]
    assert "attempt_id=attempt_seed" in icall, (
        f"the initial site no longer passes attempt_seed: {icall!r}"
    )
    assert "attempt_id=attempt_seed" in src[src.index("AttemptReasoningRuntime(") :][:200], (
        "the runtime is no longer constructed with attempt_seed, so the initial and "
        "per-observation digests would use DIFFERENT attempt identities and never match"
    )
