"""Covering-RED attribution must never blame the edit on bare basename equality.

RED-FIRST (2026-07-29, W2-R3). Baseline defect, pinned before the fix:
``native_render.is_edit_attributed`` kept a basename fallback —

    return fp in ef or os.path.basename(fp) in {os.path.basename(e) for e in ef}

— so a traceback whose deepest non-test frame lands in a DEPENDENCY file that
merely shares a basename with an edited file (``utils.py`` being the classic:
``/usr/lib/python3/site-packages/somedep/utils.py`` vs the agent's
``src/mypkg/utils.py``) attributed the covering RED to the agent's edit, and
the false edit-blame shipped as Format-D through
``covering_runner.is_red_attributable``.

The fix: attribution requires an exact normalized-path match or a path-suffix
match ANCHORED at a path separator where the shorter side itself carries a
directory segment — never bare basename equality. A basename-only coincidence
is NOT attribution; an unattributed RED stays quiet (existing downstream
behavior), which is exactly correct-or-quiet: wrong blame is worse than no
blame.

MUTATION TARGETS:
  * restore the basename fallback -> the dependency-frame tests bite;
  * drop the multi-segment ("/" in shorter) requirement from the suffix rule
    -> the single-segment ``utils.py`` test bites;
  * drop the suffix rule entirely (exact-only) -> the container-absolute-path
    attribution test bites.
"""

from __future__ import annotations

from groundtruth.runtime import covering_runner as cr
from groundtruth.runtime import native_render as nr


def _red(stdout_tail: str) -> dict:
    return {
        "verdict": "fail",
        "exit_code": 1,
        "stdout_tail": stdout_tail,
        "stderr_tail": "",
    }


_DEP_FRAME_CRASH = (
    "=================================== FAILURES ===================================\n"
    "_________________________________ test_refund _________________________________\n"
    "tests/test_pay.py:12: in test_refund\n"
    "    result = transform(5)\n"
    "/usr/lib/python3/site-packages/somedep/utils.py:33: in coerce\n"
    "    return int(value)\n"
    "E   ValueError: invalid literal for int()\n"
    "FAILED tests/test_pay.py::test_refund - ValueError"
)


def test_dependency_frame_sharing_basename_is_not_attributed() -> None:
    """THE utils.py FALSE BLAME: the deepest non-test frame is a DEPENDENCY
    file; only the basename coincides with the edited file. Never attribution."""
    r = _red(_DEP_FRAME_CRASH)
    assert nr.is_edit_attributed(
        r, ["src/mypkg/utils.py"], test_files=["tests/test_pay.py"],
    ) is False


def test_is_red_attributable_stays_quiet_on_basename_coincidence() -> None:
    """The seam's ONE question must give the same answer on the frames-only
    path (no repo_root / covering_files => no differential leg)."""
    r = _red(_DEP_FRAME_CRASH)
    assert cr.is_red_attributable(
        r, ["src/mypkg/utils.py"], test_files=["tests/test_pay.py"],
    ) is False


def test_single_segment_edited_file_is_not_blamed_for_dep_frame() -> None:
    """A repo-root edited ``utils.py`` must not match a pathed dependency
    frame via an anchored suffix either — a bare basename never attributes."""
    r = _red(_DEP_FRAME_CRASH)
    assert nr.is_edit_attributed(
        r, ["utils.py"], test_files=["tests/test_pay.py"],
    ) is False


def test_container_absolute_frame_still_attributes_repo_relative_edit() -> None:
    """The HONEST case the basename fallback papered over: the frame carries
    the container-absolute form of the edited repo-relative path. A suffix
    match anchored at '/' (shorter side multi-segment) attributes it."""
    crash = (
        "tests/test_pay.py:12: in test_refund\n"
        "    result = transform(5)\n"
        "/testbed/src/mypkg/utils.py:88: in transform\n"
        "    return a + b\n"
        "E   TypeError: unsupported operand type(s)\n"
    )
    r = _red(crash)
    assert nr.is_edit_attributed(
        r, ["src/mypkg/utils.py"], test_files=["tests/test_pay.py"],
    ) is True


def test_exact_relative_frame_match_still_attributes() -> None:
    """The plain exact-path case is untouched."""
    crash = (
        "tests/test_pay.py:12: in test_refund\n"
        "src/mymodule.py:88: in transform\n"
        "E   TypeError: boom\n"
    )
    r = _red(crash)
    assert nr.is_edit_attributed(
        r, ["src/mymodule.py"], test_files=["tests/test_pay.py"],
    ) is True
