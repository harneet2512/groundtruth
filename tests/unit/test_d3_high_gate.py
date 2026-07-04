"""D-3 calibration: HIGH localization tier downgrades a lone-edge function pick.

Ramp defect D-3 (2026-06-04): the HIGH tier named `func` = the anchor of ONE
max-strength issue edge. An issue anchor is a symbol NAMED in the issue, often a
REFERENCED symbol (the far end of a CALLS edge) rather than the function to edit
(sh-744: HIGH said `stdout`, gold was `__await__`). A confident-wrong function is
the worst failure mode (The Distracting Effect, arXiv:2505.06914). `_high_func_support`
keeps HIGH only when >=2 distinct STRUCTURAL witnesses converge on the named func.

These are pure-logic unit tests of the helper (NOT a brief-quality measurement — per
BRIEFING.md §5 brief numbers come from measure_brief.py on generate_v1r_brief with
semantic ON; the sh-744 HIGH->MEDIUM downgrade is confirmed on the real run before the
full benchmark).
"""
from groundtruth.pretask.v1r_brief import _high_func_support
from groundtruth.pretask.graph_localizer import Witness


def _w(anchor, direction, src, dst, edge_type="CALLS"):
    return Witness(
        file_path="sh.py", anchor=anchor, edge_type=edge_type, direction=direction,
        verified=True, confidence=1.0, hop=1, src_symbol=src, dst_symbol=dst,
    )


def test_lone_edge_anchor_is_weak_downgrades():
    # sh-744 shape: HIGH named `stdout`, supported by exactly one "wait calls stdout" edge.
    witnesses = [_w("stdout", "calls_anchor", "wait", "stdout"),
                 _w("wait", "calls_anchor", "__await__", "wait")]
    assert _high_func_support(witnesses, "stdout") == 1
    assert _high_func_support(witnesses, "stdout") < 2  # -> HIGH downgrades to MEDIUM


def test_multi_edge_convergence_keeps_high():
    witnesses = [_w("foo", "calls_anchor", "bar", "foo"),
                 _w("foo", "called_by_anchor", "foo", "baz"),
                 _w("other", "calls_anchor", "x", "other")]
    assert _high_func_support(witnesses, "foo") == 2  # -> HIGH stays


def test_defines_witness_is_not_structural_support():
    # A defines_anchor (issue names a function defined here) is NOT a structural edge:
    # it must not, alone, justify the imperative HIGH steer.
    witnesses = [Witness("f.py", "foo", "DEFINES", "defines_anchor", True, 1.0, 0, "foo", "foo")]
    assert _high_func_support(witnesses, "foo") == 0


def test_case_insensitive_and_empty_safe():
    witnesses = [_w("stdout", "calls_anchor", "wait", "stdout")]
    assert _high_func_support(witnesses, "STDOUT") == 1
    assert _high_func_support([], "x") == 0
    assert _high_func_support(None, "x") == 0


def test_duplicate_views_of_one_edge_count_once():
    # Same edge identity twice must not inflate support to 2.
    w = _w("foo", "calls_anchor", "bar", "foo")
    assert _high_func_support([w, w], "foo") == 1
