"""Byte-parity test for the two LIVE-for-DeepSWE test/demo path-filter copies.

THE DUPLICATION (the two copies that ship on the DeepSWE path; the OH/v7 ones
are out of scope here):

  * BRIEF side  -- ``src/groundtruth/pretask/v1r_brief.py::_is_test_path``
    (~451). Catches TEST dir-segments + basename test markers. MISSES demo /
    non-source dirs (examples/, docs_src/, tutorial/, fixtures/, vendor/,
    node_modules/, dist/, build/).

  * PER-TURN side -- ``artifact_deepswe/gt_mini_patch.py::_is_test_or_demo_path``
    (~362). The SUPERSET: TEST dir-segments + basename markers OR demo /
    non-source dir-segments. This is the CORRECT canonical behavior.

THE DRIFT (witnessed): because the brief copy under-catches demo dirs, the
localization candidate set leaked ``docs_src/`` files in the fastapi run
(``docs_src/tutorial001.py`` surfaced as a candidate edit target).

THE CANONICAL UNION PREDICATE (target state, == the twin's logic):

    is_test_or_demo(path) :=
        any test-dir segment in the path's DIRECTORY segments
        OR any demo/non-source dir segment in the directory segments
        OR a basename test marker (test_*, *_test.*, *.test.*, *.spec.*)

i.e. ``gt_mini_patch._is_test_or_demo_path`` is the superset and is correct;
the de-dup points BOTH call sites at one predicate with this behavior.

This test PINS that target. Rows where the brief copy currently UNDER-catches
(demo dirs) are marked ``xfail`` against the brief predicate -- they fail RED
today (documenting the drift) and FLIP GREEN (xpass) once the de-dup lands and
the brief side calls the union predicate. Read an unexpected xpass as: the
de-dup happened, delete the xfail marker.

Run:  cd /d/Groundtruth && python -m pytest tests/test_test_filter_parity.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_brief_predicate():
    """``v1r_brief._is_test_path`` -- the BRIEF-side copy (under-catches demo)."""
    src_dir = os.path.join(_REPO_ROOT, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from groundtruth.pretask.v1r_brief import _is_test_path  # type: ignore

    return _is_test_path


def _load_twin_predicate():
    """``gt_mini_patch._is_test_or_demo_path`` -- the PER-TURN copy (the superset).

    Loaded by file path: ``artifact_deepswe`` is not an importable package and
    the module is a pure, dependency-free leaf (its own docstring promises "no
    deps"), so a file-spec import is the honest way to reach it.
    """
    path = os.path.join(_REPO_ROOT, "artifact_deepswe", "gt_mini_patch.py")
    spec = importlib.util.spec_from_file_location("gt_mini_patch", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._is_test_or_demo_path  # type: ignore[attr-defined]


brief_is_test = _load_brief_predicate()
twin_is_test_or_demo = _load_twin_predicate()


# (path, should_be_filtered, drifts_on_brief_today)
#
# should_be_filtered = the UNIFIED target: a test file OR a demo/non-source file
#   is filtered (True); a real source file is kept (False).
# drifts_on_brief_today = True only for the demo-dir rows the brief copy MISSES
#   right now -- these are xfail'd against the brief predicate.
#
# Anti-substring negatives ``contest/a.py`` / ``latest/b.py`` pin that BOTH
# predicates match DIRECTORY SEGMENTS, not the ``test`` substring -- a "contest"
# or "latest" dir must never be filtered as a test dir.
_CASES = [
    # path                          should_be   brief_drifts
    ("test/lexer.js", True, False),  # top-level test dir (csstree)
    ("__tests__/awilix.test.ts", True, False),  # JS test dir + .test. marker
    # de-dup'd 2026-06-15: brief now delegates to path_policy.is_test_or_demo, so these
    # DEMO-dir rows are CAUGHT (was the fastapi docs_src candidate leak). drift -> False.
    ("docs_src/tutorial001.py", True, False),  # DEMO dir -> brief now catches
    ("examples/demo.js", True, False),  # DEMO dir -> brief now catches
    ("lib/utils/List.js", False, False),  # real source -> kept
    ("src/container.ts", False, False),  # real source -> kept
    ("src/test_helpers/x.go", False, False),  # 'test_helpers' is NOT a test dir seg
    ("contest/a.py", False, False),  # substring 'test' != test dir seg
    ("latest/b.py", False, False),  # substring 'test' != test dir seg
]


# --- The twin IS the canonical predicate: it must already match the target on
#     every row, with no xfails. If a twin row regresses, the chosen canonical
#     predicate is wrong and the de-dup target is unsafe. ---
@pytest.mark.parametrize(
    "path,should_be",
    [(p, s) for (p, s, _drift) in _CASES],
    ids=[p for (p, _s, _d) in _CASES],
)
def test_twin_predicate_is_the_canonical_union(path: str, should_be: bool) -> None:
    """The PER-TURN twin already implements test+demo union -> it is the de-dup
    target. No xfail anywhere: every row must match the unified behavior today."""
    assert twin_is_test_or_demo(path) is should_be, (
        f"canonical twin disagrees with target on {path!r}: "
        f"got {twin_is_test_or_demo(path)}, want {should_be}"
    )


# --- The brief predicate must reach the SAME target. Demo-dir rows are xfail'd
#     because the brief copy under-catches them TODAY; the de-dup (point the
#     brief side at the union predicate) flips these to xpass. ---
def _brief_param(path: str, should_be: bool, drifts: bool):
    marks = ()
    if drifts:
        marks = (
            pytest.mark.xfail(
                reason="brief copy missing demo dirs until de-dup "
                "(point v1r_brief._is_test_path at the union predicate)",
                strict=True,
            ),
        )
    return pytest.param(path, should_be, marks=marks, id=path)


@pytest.mark.parametrize(
    "path,should_be",
    [_brief_param(p, s, d) for (p, s, d) in _CASES],
)
def test_brief_predicate_reaches_union_target(path: str, should_be: bool) -> None:
    """Brief side must match the unified test+demo target.

    Demo-dir rows (``docs_src/``, ``examples/``) are ``xfail(strict=True)``:
    they FAIL today (the drift) and become XPASS the moment the de-dup points
    the brief side at the union predicate. A strict xfail that XPASSes turns
    the suite RED -- forcing the marker's removal, so the test cannot rot into
    silently passing on the old broken behavior."""
    assert brief_is_test(path) is should_be, (
        f"brief predicate disagrees with union target on {path!r}: "
        f"got {brief_is_test(path)}, want {should_be}"
    )


# --- Direct parity pin: once de-dup lands, the two predicates must agree on
#     EVERY non-drift row TODAY, and on ALL rows after de-dup. The drift rows
#     are the only allowed disagreement now, and they are exactly the xfail'd
#     ones above -- so this asserts agreement on the rest. ---
@pytest.mark.parametrize(
    "path,should_be",
    [(p, s) for (p, s, d) in _CASES if not d],
    ids=[p for (p, _s, d) in _CASES if not d],
)
def test_predicates_already_agree_on_non_drift_rows(path: str, should_be: bool) -> None:
    """On every row that is NOT a known brief-drift case, the two copies already
    return the SAME value -- and that value is the union target. This is the
    parity the de-dup must preserve while it closes the two demo-dir rows."""
    assert brief_is_test(path) == twin_is_test_or_demo(path) == should_be, (
        f"parity broken on supposedly-agreeing row {path!r}: "
        f"brief={brief_is_test(path)} twin={twin_is_test_or_demo(path)} "
        f"want={should_be}"
    )
