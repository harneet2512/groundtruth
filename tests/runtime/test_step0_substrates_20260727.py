"""Step-0 staging must offer the substrates its evidence actually requires.

THE BUG. `_stage_initial_canonical_evidence` calls `prepare_next_inference` with a HARDCODED
``available_substrates=("graph", "brief_result")``. The obligations contract requires
preferred ``("issue_text", "obligation_parser")`` or fallback
``("exact_issue_text", "canonical_task_event")`` (`reasoning_runtime.py` fallback policy).
The intersection is EMPTY, so `evaluate_feature_contract` returns HELD /
``PREREQUISITES_PENDING`` and the step-0 obligations record cannot be released.

WHY IT MATTERS. `obligations` is the only standing carrier of BEHAVIORAL_CONTRACT, which is
the required role of both PATCH_CONSTRUCTION and SOURCE_UNDERSTANDING. Step 0 is also the
ONLY cycle in which that record is ever fresh (a separate defect makes `runtime_evidence`
change every observation). So this one hardcoded tuple is the difference between the oracle
being able to compose its first capsule and never composing one at all: measured across two
full runs, `unresolved_roles` is BEHAVIORAL_CONTRACT on 70 of 90 compile attempts.

THE FIX. Derive the offered substrates from the records being staged, exactly as the
per-observation path already does via `_available_substrates(records)`. Do not hand-extend
the hardcoded tuple: a literal that must be kept in sync with the fact registry by hand is
the same defect one edit later.
"""

from __future__ import annotations

import inspect

from artifact_deepswe import gt_mini_patch as seam


def _staging_source() -> str:
    return inspect.getsource(seam._stage_initial_canonical_evidence)


def test_staging_does_not_hardcode_a_substrate_tuple():
    """THE FIX. The offered substrates must come from the records, not a literal."""
    src = _staging_source()
    # Match the ARGUMENT form, not the bare literal: the fix's own comment quotes the old
    # tuple to explain the defect, and a naive `literal not in src` fails on that comment --
    # a test that cannot survive its own documentation is testing the wrong thing.
    assert 'available_substrates=("graph"' not in src, (
        "step-0 staging still PASSES a hardcoded substrate tuple that shares NOTHING with "
        "what the obligations contract requires -- the record is HELD on the only cycle it "
        "is ever fresh, and BEHAVIORAL_CONTRACT is never carried"
    )


def test_staging_derives_substrates_from_the_records():
    """It must use the same derivation the per-observation path uses, so the two cannot
    drift apart."""
    src = _staging_source()
    assert "_available_substrates" in src, (
        "staging does not derive substrates from the staged records; the per-observation "
        "path already has `_available_substrates(records)` and both must agree"
    )


def test_the_derivation_helper_still_exists_and_is_used_per_observation():
    """POSITIVE CONTROL. If `_available_substrates` were renamed or removed, the assertion
    above would be satisfiable by a stale reference that never runs."""
    # It is a member of CanonicalRuntimeAttachment, not a module-level function.
    helper = getattr(seam.CanonicalRuntimeAttachment, "_available_substrates", None)
    assert helper is not None, (
        "_available_substrates is gone -- the fix above would be pointing at nothing"
    )


def test_obligations_requirements_are_what_we_think():
    """POSITIVE CONTROL on the PREMISE. If the obligations fallback policy changes, this
    whole test file is reasoning about the wrong contract and must be revisited."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import groundtruth.runtime.reasoning_runtime as rr

    src = inspect.getsource(rr)
    assert '"obligations": (' in src
    assert '("issue_text", "obligation_parser")' in src, (
        "the obligations substrate requirement changed; re-derive the staging fix"
    )
