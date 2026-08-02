r"""Fable-LIPI round-2 (2026-07-11), Invariant-1 (CONFIRMED-VIOLATED, MODERATE): the value-line
keep branch of ``render_covering_failure_native`` leaks a MID-LINE test-file path.

The whole-line ``_is_test_path(s, tf)`` guard misses a token like ``right: 3', tests/x.rs:88:5``
because ``_TEST_DIR_RE``'s ``(^|/)`` anchor fails after a space/comma, and ``_final_scrub`` had no
test-path leg at all. So the runner's own value/panic line — kept for its fix-signal delta — ships
the covering test's PATH onto the model-facing surface. The frame branch already strips per-path;
the value branch did not.

The fix threads the ran-set ``tf`` into ``_final_scrub`` and scrubs any embedded ``path[:line[:col]]``
token whose file is a test file — by CONVENTION (``tests/``) OR by the RAN-SET (a covering test GT
actually ran that lives in a NON-conventional dir like ``qa/``, which convention can't know). The
agent's own non-test source frame (the where-to-fix) must survive untouched.

Not covered by ``test_b1_verification_execution.py`` — that pin exercises the cargo path only with a
``src/lib.rs`` frame and an EMPTY ``test_files`` set, which sidesteps this leak entirely.
"""
from __future__ import annotations

from pathlib import Path
import sys

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

from groundtruth.runtime.native_render import (  # noqa: E402
    render_covering_failure_native,
    contains_test_identity,
)


def _render(stderr: str, test_files):
    return render_covering_failure_native({"stdout_tail": "", "stderr_tail": stderr},
                                          test_files=test_files)


def test_midline_convention_testpath_scrubbed():
    # `tests/` is a conventional test dir; the token sits mid-line after a comma+space, so the
    # whole-line guard misses it. The value delta (`right: 3`) must survive.
    out = _render("assertion `left == right` failed\n  right: 3', tests/harness_checks.rs:120:9",
                  ["tests/harness_checks.rs"])
    assert "tests/harness_checks.rs" not in out, out
    assert not contains_test_identity(out, {"tests/harness_checks.rs"}), out
    assert "right: 3" in out or "left == right" in out, out


def test_midline_ranset_nonconventional_testpath_scrubbed():
    # `qa/` is NOT a conventional test dir — only the RAN-SET knows it is the covering test.
    # Honoring `tf` (not convention alone) is what closes this case.
    out = _render("got 5, want 3 (see qa/flow_scenarios.go:42)", ["qa/flow_scenarios.go"])
    assert "qa/flow_scenarios.go" not in out, out
    assert not contains_test_identity(out, {"qa/flow_scenarios.go"}), out
    assert "got 5, want 3" in out, out


def test_agent_own_source_frame_preserved():
    # The scrub must be surgical: a NON-test source frame (the agent's where-to-fix) survives.
    out = _render("thread 'x' panicked at src/pool.rs:88:5:\n"
                  "called `Option::unwrap()` on a `None` value",
                  ["tests/pool_test.rs"])
    assert "src/pool.rs" in out, out
    assert not contains_test_identity(out, {"tests/pool_test.rs"}), out
