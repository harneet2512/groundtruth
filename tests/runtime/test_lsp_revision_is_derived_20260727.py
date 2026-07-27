"""The `lsp` revision dimension is DERIVED, never configured by an environment variable.

THE DEFECT. Both revision-stamping sites in the seam preferred a `GT_LSP_REVISION` environment
variable and fell back to a digest derived from the repository content::

    lsp = os.environ.get("GT_LSP_REVISION", "").strip() or sha256(f"lsp-unavailable:{repo}")

That branch was dead on BOTH ends:

  * nothing in the workflows, scripts or seam ever SET the variable, and it was never
    --ae-forwarded, so it could not be set inside the container even in principle; and
  * no feature contract carries `lsp` as a revision dependency, so the dimension it feeds is
    stamped onto the RevisionVector but never consulted for freshness.

It was caught by `test_r1_ae_parity_invariant_failclosed` -- the --ae parity guard, which fails
closed on any GT_* variable the seam READS but nobody forwards. The guard was right, and the
tempting fix (add it to the --ae forward list, or park it on the pending-forward seam) would
have silenced the guard while KEEPING the dead configuration surface -- precisely the outcome
the guard exists to prevent. The read was removed instead.

This file pins the outcome so the phantom knob cannot come back, and pins the premises that
make removal (rather than forwarding) the correct call -- if either premise stops holding, this
reasoning must be redone rather than silently inherited.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from artifact_deepswe import gt_mini_patch as seam

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_the_environment_read_is_gone_from_both_sites():
    """THE FIX. Neither stamping site may consult the environment for this dimension."""
    src = inspect.getsource(seam)
    # Exclude the helper's own docstring, which quotes the removed expression to explain the
    # defect -- a test that cannot survive its own documentation is testing the wrong thing.
    helper = inspect.getsource(seam._lsp_revision)
    assert "GT_LSP_REVISION" in helper, (
        "POSITIVE CONTROL: the helper docstring no longer explains the removed read, so the "
        "exclusion below could pass vacuously against any source text"
    )
    outside = src.replace(helper, "")
    assert "GT_LSP_REVISION" not in outside, (
        "a GT_LSP_REVISION read survives outside the helper docstring -- the seam reads a "
        "variable nothing forwards, which the --ae parity invariant fails closed on"
    )


def test_both_sites_use_the_one_shared_helper():
    """Two formulas that must agree by hand is the defect class this replaces."""
    src = inspect.getsource(seam)
    assert src.count("_lsp_revision(") >= 3, (
        "expected 1 definition + 2 call sites; the initial and per-observation stamps must "
        "not drift apart"
    )


def test_the_helper_is_deterministic():
    assert seam._lsp_revision("repo-1") == seam._lsp_revision("repo-1")


def test_the_helper_still_moves_when_the_repository_moves():
    """ANTI-WEAKENING. A constant would make lsp-stamped evidence eternally fresh."""
    assert seam._lsp_revision("repo-1") != seam._lsp_revision("repo-2")


def test_the_helper_ignores_the_phantom_knob(monkeypatch: pytest.MonkeyPatch):
    """BEHAVIOURAL, not structural. Even with the variable set, the value must not change --
    a source-text assertion alone would pass against a helper that still read os.environ
    through some other spelling."""
    baseline = seam._lsp_revision("repo-1")
    monkeypatch.setenv("GT_LSP_REVISION", "an-externally-supplied-revision")
    assert seam._lsp_revision("repo-1") == baseline, (
        "the environment still steers the lsp revision dimension"
    )


def test_premise_no_contract_depends_on_the_lsp_dimension():
    """POSITIVE CONTROL ON THE PREMISE, and the condition for revisiting this decision.

    Removal (rather than forwarding) is correct only while nothing grades freshness on `lsp`.
    The control is the second assertion: the dependency VOCABULARY must still contain `lsp`,
    so this test fails loudly if the mapping is deleted rather than passing vacuously.
    """
    import groundtruth.runtime.reasoning_runtime as rr

    facts = (
        "caller_contract", "covering_red", "def_partition", "localization",
        "newfile_precedent", "obligations", "recovery", "signature_delta",
        "submit_refusal", "syntax_result",
    )
    checked = 0
    for feature in facts:
        contract = rr.feature_contract_for(feature)
        if contract is None:  # pragma: no cover - registry drift
            continue
        checked += 1
        assert "lsp" not in contract.revision_dependencies, (
            f"{feature} now grades freshness on the lsp dimension -- that dimension is a "
            "derived placeholder with no real source, so this fix must be revisited"
        )
    assert checked == len(facts), f"only {checked}/{len(facts)} contracts resolved"
    assert rr._REVISION_DEPENDENCY_DIMENSION.get("lsp") == "lsp", (
        "the lsp dependency mapping was removed; re-derive this decision"
    )
