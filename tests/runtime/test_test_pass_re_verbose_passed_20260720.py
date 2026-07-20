"""D-U (run6 batch6, telegram): the l5.no_test / verify.horizon nudge asserted
'You have NOT observed a single test execute' on an observation carrying 100+
pytest `-v` PASSED lines. Root cause = TEST_PASS_RE recognized the *summary*
form (`\\d+ passed`) but NOT the uppercase per-test token `PASSED`, while
TEST_FAIL_RE already recognized the symmetric `FAILED`. When the summary tail
is truncated away (streaming / tail-capped observation), the pass detector goes
blind and the correct-or-quiet rule is violated (false info is worse than none).

Fix = add the symmetric `\\bPASSED\\b` token to TEST_PASS_RE (canonical
patterns.py + the identical inline fallback in gt_mini_patch.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from groundtruth.runtime.patterns import TEST_PASS_RE, TEST_FAIL_RE  # noqa: E402
from groundtruth.runtime.verification_horizon import (  # noqa: E402
    render_verify_emission,
)


def _verbose_passed_block(n: int = 119) -> str:
    # pytest -v per-test lines, summary line truncated away (the telegram shape)
    return "\n".join(
        f"tests/test_mod.py::test_case_{i} PASSED [ {min(i, 99)}%]"
        for i in range(1, n + 1)
    )


def test_uppercase_per_test_passed_is_recognized():
    # the bug: 100+ PASSED lines with no summary must count as observed test evidence
    assert TEST_PASS_RE.search(_verbose_passed_block()) is not None, (
        "uppercase per-test PASSED token must be recognized as a pass result — "
        "else l5.no_test falsely claims no test executed")


def test_pass_fail_token_symmetry():
    # FAIL side already recognizes uppercase FAILED; PASS side must be symmetric
    assert TEST_FAIL_RE.search("tests/test_mod.py::test_x FAILED [ 10%]") is not None
    assert TEST_PASS_RE.search("tests/test_mod.py::test_x PASSED [ 10%]") is not None


def test_summary_form_still_matches():
    # regression guard: the previously-recognized summary form must keep matching
    assert TEST_PASS_RE.search("==== 119 passed in 5.21s ====") is not None
    assert TEST_PASS_RE.search("test result: ok. 12 passed; 0 failed") is not None


def test_plain_prose_passed_not_over_greedy_on_failure():
    # a mixed run (some PASSED, some FAILED) must still register as a fail-bearing
    # result so the nudge stays silent AND green-verdict consumers see the failure
    mixed = "test_a PASSED [ 50%]\ntest_b FAILED [100%]"
    assert TEST_FAIL_RE.search(mixed) is not None  # failure still detected first


# --- D-U part (b): verify.horizon advisory must not assert coverage it cannot ground ---

def test_advisory_does_not_assert_uncovered_tests():
    # has_covering False (no graph-linked covering test): must NOT claim tests
    # cover the edit (the false 'relevant tests cover them' for state.js)
    out = render_verify_emission("advisory", 20, 100, {"state.js"}, covering_tests=None)
    assert "cover them" not in out and "covers them" not in out, (
        "ungrounded coverage assertion when no covering test is known")
    assert "state.js" in out  # still names the edit and advises verification


def test_advisory_asserts_coverage_only_with_graph_link():
    out = render_verify_emission(
        "advisory", 20, 100, {"state.js"}, covering_tests=["t_state"])
    assert "covers them" in out  # grounded on a graph-linked covering test
