"""The issue text cannot change during an attempt, so `issue` must not be a mutable dep.

THE REMAINING HOLE. `_REVISION_DEPENDENCY_DIMENSION` maps ``"issue" -> "runtime_evidence"``.
Unifying the runtime_evidence formula stopped it churning on every observation, but it still
moves whenever the repository content or graph digest moves -- i.e. **on every edit**.

Why that is fatal exactly where it matters most: `obligations` is the only standing carrier
of BEHAVIORAL_CONTRACT, and BEHAVIORAL_CONTRACT is the required role of PATCH_CONSTRUCTION.
PATCH_CONSTRUCTION is the phase the agent enters AFTER editing. So the single decision that
most needs the obligations record is the decision at which the record has just been
invalidated by the edit that created the decision. The oracle would still never compose a
capsule on any task where the agent edits -- which is every real task.

WHY `issue` IS DIFFERENT FROM ITS NEIGHBOURS. `patch_rev`, `edit_rev` and `episode_state` are
genuinely derived from mutable runtime state; they SHOULD retire when the repository moves.
`issue` is the problem statement handed to the attempt at task start. It is fixed for the
lifetime of the attempt. An obligations record derived from it does not become false because
a file changed -- the requirements the fix must satisfy are the same requirements.

THE FIX. An explicit immutable-dependency set that `_evidence_revision_is_fresh` treats as
always satisfied. NOT by deleting the mapping: an unmapped dependency makes the record stale
(the function returns False for `dimension is None`), which is the opposite of intended.

DO NOT widen this to the other three. If `patch_rev` stops retiring on repository change,
GT will serve edit-derived evidence about a file that has since changed -- stale evidence
presented as fact, which is worse than silence.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import groundtruth.runtime.reasoning_runtime as rr  # noqa: E402


R1 = rr.RevisionVector(
    repository_content="repo-1", graph="g-1", lsp="l-1", runtime_evidence="rt-1"
)
R2 = rr.RevisionVector(
    repository_content="repo-2", graph="g-1", lsp="l-1", runtime_evidence="rt-2"
)


def _record(dep: str, revision):
    """A minimal evidence record carrying exactly one revision dependency."""
    import types

    return types.SimpleNamespace(revision=revision, revision_dependencies=(dep,))


def test_issue_derived_evidence_survives_a_repository_change():
    """THE FIX. The agent edits; the requirements it must satisfy do not change."""
    assert rr._evidence_revision_is_fresh(_record("issue", R1), R2) is True, (
        "obligations was invalidated by an edit -- so PATCH_CONSTRUCTION, the phase that "
        "REQUIRES BEHAVIORAL_CONTRACT and is entered right after an edit, can never have it"
    )


def test_issue_survives_even_when_every_dimension_moves():
    """Immutable means immutable: no dimension change may retire it."""
    moved = rr.RevisionVector(
        repository_content="x", graph="y", lsp="z", runtime_evidence="w"
    )
    assert rr._evidence_revision_is_fresh(_record("issue", R1), moved) is True


def test_mutable_runtime_deps_still_retire_on_a_repository_change():
    """ANTI-WEAKENING, and the line this fix must not cross. patch_rev / edit_rev /
    episode_state ARE derived from mutable state and must still go stale, or GT serves
    edit-derived evidence about a file that has since changed."""
    for dep in ("patch_rev", "edit_rev", "episode_state"):
        assert rr._evidence_revision_is_fresh(_record(dep, R1), R2) is False, (
            f"{dep} no longer retires on a repository change -- stale evidence would ship"
        )


def test_graph_and_repository_deps_are_untouched():
    """ANTI-WEAKENING. The structural dependencies must keep their exact semantics."""
    assert rr._evidence_revision_is_fresh(_record("graph", R1), R2) is True  # graph unchanged
    assert rr._evidence_revision_is_fresh(_record("repository_content", R1), R2) is False
    moved_graph = rr.RevisionVector(
        repository_content="repo-1", graph="g-2", lsp="l-1", runtime_evidence="rt-1"
    )
    assert rr._evidence_revision_is_fresh(_record("nodes", R1), moved_graph) is False


def test_an_unknown_dependency_is_still_treated_as_stale():
    """ANTI-WEAKENING / correct-or-quiet. A dependency nobody mapped must fail closed --
    that is why the fix is an explicit immutable SET and not a deletion from the map."""
    assert rr._evidence_revision_is_fresh(_record("no_such_dependency", R1), R1) is False


def test_the_immutable_set_is_explicit_and_minimal():
    """The exemption must be a named, auditable set -- not a special case buried in a
    conditional, and not silently extended."""
    immutable = getattr(rr, "_IMMUTABLE_REVISION_DEPENDENCIES", None)
    assert immutable is not None, "no explicit immutable-dependency set"
    assert "issue" in immutable
    for dep in ("patch_rev", "edit_rev", "episode_state", "graph", "repository_content"):
        assert dep not in immutable, (
            f"{dep} was added to the immutable set -- it is derived from mutable state"
        )


def _real_record(feature_id: str, revision):
    """A REAL EvidenceRecord for `feature_id`, with its dependencies taken FROM ITS CONTRACT.

    `EvidenceRecord` validates that `revision_dependencies` exactly match the feature
    contract, so a record cannot pair an arbitrary dependency with an arbitrary feature --
    a good guard, and the reason this helper derives them instead of hardcoding.
    """
    contract = rr.feature_contract_for(feature_id)
    return rr.EvidenceRecord(
        evidence_id=f"GT-E-probe-{feature_id}",
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="probe",
        claim=f"{feature_id} claim",
        actionable_consequence="act on it before commitment",
        provenance=("probe",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=revision,
        causal_neighborhood=("subject:probe",),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.RESULT_DERIVED,
    )


def test_invalidate_stale_evidence_honours_the_immutable_set():
    """THE TEST THAT WAS MISSING, and the reason it mattered.

    `invalidate_stale_evidence` has its OWN dependency loop and never calls
    `_evidence_revision_is_fresh`. The exemption was first added only to the predicate, so
    every test here passed while the live path still marked the record INVALIDATED -- the fix
    did nothing where it counts. An offline reproduction caught the contradiction (predicate
    said fresh, store said INVALIDATED). Both functions must be pinned.
    """
    out = rr.invalidate_stale_evidence(_real_record("obligations", R1), current_revision=R2)
    assert out.lifecycle is not rr.EvidenceLifecycle.INVALIDATED, (
        "the live invalidation path still retires issue-derived evidence on an edit -- "
        "PATCH_CONSTRUCTION can never obtain BEHAVIORAL_CONTRACT"
    )


def test_invalidate_stale_evidence_still_retires_mutable_deps():
    """ANTI-WEAKENING on the same function: mutable deps must still be invalidated."""
    # Features whose contracts carry the MUTABLE runtime dependencies.
    for feature in ("syntax_result", "covering_red", "recovery"):
        deps = rr.feature_contract_for(feature).revision_dependencies
        if not any(d in {"patch_rev", "edit_rev", "episode_state"} for d in deps):
            continue  # contract changed; the anti-weakening target moved
        out = rr.invalidate_stale_evidence(_real_record(feature, R1), current_revision=R2)
        assert out.lifecycle is rr.EvidenceLifecycle.INVALIDATED, (
            f"{feature} (deps {deps}) survived a repository change on the live path -- "
            "stale edit-derived evidence would ship"
        )


def test_an_unknown_dependency_cannot_even_be_CONSTRUCTED():
    """ANTI-WEAKENING, corrected to where the guard actually lives.

    This test originally tried to prove `invalidate_stale_evidence` raises on an unknown
    dependency. It cannot be written that way: `EvidenceRecord` validates that
    `revision_dependencies` exactly match the feature contract, so a record carrying an
    unmapped dependency cannot be constructed in the first place -- `dataclasses.replace`
    re-runs that validation and refuses.

    So the fail-closed guarantee is UPSTREAM of the invalidation path, and the raise inside
    `invalidate_stale_evidence` is defence-in-depth on an unreachable state. Asserting the
    reachable guard is the honest test; asserting the unreachable one would have required
    bypassing a frozen dataclass, which proves nothing about production.
    """
    import pytest
    from dataclasses import replace as _replace

    with pytest.raises(ValueError, match="revision_dependencies must exactly match"):
        _replace(_real_record("obligations", R1), revision_dependencies=("no_such_dep",))

    # And the defence-in-depth raise is still present in the invalidation path.
    src = inspect.getsource(rr.invalidate_stale_evidence)
    assert "unknown revision dependency" in src, (
        "the defence-in-depth guard was removed from invalidate_stale_evidence"
    )
