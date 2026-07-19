"""Colorized-runner robustness pins (2026-07-19 pre-run-#4 leak class).

Colorized pytest/CPython output wraps tokens in ANSI CSI sequences.  Every
anchored plain-text recognizer on the covering/verdict path was defeated by a
leading escape byte, producing two live defect directions:

* LEAK: ``_final_scrub``'s identity recognizers never matched the colorized
  test nodeid, so ``<test>::test_foo`` shipped verbatim to the model.
* ATTRIBUTION: verdict counts, failing-name extraction, and deepest-frame
  attribution all silently returned empty -> covering RED suppressed.

The original B1 reds only exercised this when the host environment leaked
color into the inner pytest run; these pins feed explicitly colorized bytes so
the class stays covered in every environment.
"""
from __future__ import annotations

from groundtruth.runtime.native_render import (
    _final_scrub,
    deepest_agent_frame,
    render_covering_failure_native,
)
from groundtruth.runtime.test_runner import (
    _parse_failing_test_names,
    _parse_test_output,
)

_COLOR_SUMMARY = (
    "\x1b[1m=========================== short test summary info"
    " ===========================\x1b[0m\n"
    "\x1b[31mFAILED\x1b[0m test_mod.py::test_foo - assert 1 == 2\n"
    "\x1b[31m\x1b[1m1 failed\x1b[0m, \x1b[32m2 passed\x1b[0m\x1b[31m in 0.03s\x1b[0m\n"
)


def test_parse_test_output_counts_colorized_summary() -> None:
    counts = _parse_test_output(_COLOR_SUMMARY, ["pytest"])
    assert counts["failed"] == 1
    assert counts["passed"] == 2


def test_parse_failing_test_names_extracts_colorized_nodeid() -> None:
    assert _parse_failing_test_names(_COLOR_SUMMARY) == ["test_mod.py::test_foo"]


def test_deepest_agent_frame_matches_colorized_frame() -> None:
    result = {
        "stdout_tail": (
            "\x1b[1m\x1b[31mtest_mod.py\x1b[0m:4: in test_foo\n"
            "\x1b[1m\x1b[31mmod.py\x1b[0m:2: \x1b[1mTypeError\x1b[0m\n"
        ),
        "stderr_tail": "",
    }
    assert deepest_agent_frame(result, ["test_mod.py"]) == ("mod.py", 2)


def test_rendered_covering_failure_never_carries_escape_bytes() -> None:
    result = {
        "verdict": "red",
        "stdout_tail": _COLOR_SUMMARY
        + "\x1b[1m\x1b[31mmod.py\x1b[0m:2: \x1b[1mTypeError\x1b[0m: unsupported\n",
        "stderr_tail": "",
        "failing_tests": ["test_mod.py::test_foo"],
    }
    rendered = render_covering_failure_native(
        result, test_files=["test_mod.py"], edited_symbol="foo"
    )
    assert "\x1b" not in rendered
    assert "test_foo" not in rendered and "::" not in rendered


def test_final_scrub_masks_colorized_test_identity() -> None:
    line = "\x1b[31mFAILED\x1b[0m test_mod.py::test_foo - assert 1 == 2"
    scrubbed = _final_scrub(line, {"test_mod.py"})
    assert "test_foo" not in scrubbed and "::" not in scrubbed
    assert "\x1b" not in scrubbed
