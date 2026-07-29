"""An agent that validates without a named test runner must still reach SS-2.

WHY THIS IS A DELIVERY PROPERTY, NOT A SEMANTIC GATE.  ``_classify_test_observation``
recognises named test RUNNERS (pytest / go test / jest / ...) and returns ``("", "")``
for everything else.  The seam then gates a whole block on::

    if _test_outcome in {"pass", "fail", "env_fail"}:

and SS-2's submit-RED latch ``_ss_last_failing_test`` is set by ``_ss_record_test``
INSIDE that block.  So an agent that reproduces a bug with ``python -c`` or a bare
``python repro.py`` and never invokes a named runner leaves the latch unset -- and
``submit_refusal`` / ``GT_SS_SUBMIT_RED`` cannot fire at all, for the whole trajectory.
That is the measured mechanism of the cfn-lint-3764 miss: the feature was not
suppressed by a referee, it was never reachable.

Three shapes were measured returning ``("", "")`` on real trajectories:

    python -c "...api.lint_all(t)"   AttributeError + rc 1
    python repro.py                  Traceback/AssertionError + rc 1
    go build ./...                   undefined: ResolveForEach + rc 1

``classify_validation_observation`` is now consulted as a FALLBACK only.  The frozen
formal-runner classifier still wins every overlap (the new function delegates to it
first), and ``outcome`` deliberately reuses the same ``pass``/``fail``/``env_fail``
grammar so no consumer needs a new vocabulary.

This test pins REACHABILITY -- that the gate opens for these shapes and stays shut for
a non-validation command.  It does NOT claim the feature delivered anything to a model;
no offline test can establish that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gt_mini_patch as g  # noqa: E402


#: The seam's gate. SS-2's latch lives inside it.
_SS_GATE = {"pass", "fail", "env_fail"}


def _effective_outcome(cmd: str, out: str, rc: int) -> str:
    """What the seam computes: formal runner first, broadened vocabulary as fallback."""
    formal, _protocol = g._classify_test_observation(cmd, out, rc)
    if formal:
        return formal
    vobs = g._classify_validation_observation(cmd, out, rc, repo_root="")
    return getattr(vobs, "outcome", "") or ""


@pytest.mark.parametrize(
    "cmd,out,rc",
    [
        ('python -c "import api; api.lint_all(t)"', "AttributeError: boom", 1),
        ("python repro.py", "Traceback (most recent call last):\nAssertionError", 1),
        ("go build ./...", "pkg/lint/rules.go:88:14: undefined: ResolveForEach", 1),
    ],
    ids=["python-c-probe", "ad-hoc-repro", "compiler-check"],
)
def test_a_validation_without_a_named_runner_reaches_the_ss_gate(cmd, out, rc):
    """THE FIX. Each of these returned ("","") before, so SS-2 was unreachable."""
    assert g._classify_test_observation(cmd, out, rc)[0] == "", (
        "fixture precondition: the formal-runner classifier must NOT recognise this"
    )
    assert _effective_outcome(cmd, out, rc) in _SS_GATE


def test_a_named_runner_is_unchanged():
    """NEAR-NEGATIVE. The frozen classifier wins every overlap; this is a FALLBACK."""
    assert g._classify_test_observation("pytest -q", "1 failed", 1)[0] == "fail"
    assert _effective_outcome("pytest -q", "1 failed", 1) == "fail"
    assert _effective_outcome("pytest -q", "5 passed", 0) == "pass"


@pytest.mark.parametrize(
    "cmd,out,rc",
    [
        ("ls -la", "a b c", 0),
        ("cat notes.md", "SyntaxError: mentioned in prose", 1),
        ("git status", "  modified:   a/x.py", 0),
    ],
    ids=["listing", "reading-a-file-about-errors", "vcs"],
)
def test_a_non_validation_command_does_NOT_open_the_gate(cmd, out, rc):
    """ANTI-WIDENING, and the failure mode this class is most prone to.

    ``cat notes.md`` is the sharp one: its OUTPUT contains ``SyntaxError`` while the
    command validates nothing. Opening the gate here would arm the submit-RED latch
    off a file the agent merely read, and block a correct submission.
    """
    assert _effective_outcome(cmd, out, rc) not in _SS_GATE


def test_the_fallback_is_actually_wired_into_the_seam():
    """ANTI-DRIFT. The API existed for a while as dead code -- nothing imported it.

    Checked on the real module attribute, so a broken import that silently fell back
    to the ImportError stub (which returns None) fails here rather than degrading the
    seam to its previous blindness.
    """
    fn = getattr(g, "_classify_validation_observation", None)
    assert fn is not None, "the seam never imported the broadened classifier"
    assert fn.__module__ == "groundtruth.runtime.patterns", (
        f"the seam is using the ImportError stub ({fn.__module__}), so every "
        f"non-runner validation is still invisible to SS-2"
    )
