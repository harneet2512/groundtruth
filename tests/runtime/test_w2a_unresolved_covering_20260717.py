"""W2a — the EXECUTED covering-RED must fire on an INCOMPLETE FIX of a pre-existing
covered failure (base-RED + current-RED on the SAME named test), not only on a
regression (base-GREEN -> current-RED).

Root cause (062c06e80): ``_differential_attribution_result`` attributed a covering
RED ONLY when ``base_verdict == "pass"``. The dominant SWE shape is the FAIL_TO_PASS
target that pre-exists the fix — base RED, and STILL red after a locally-green-but-
incomplete edit — so the covering RED was suppressed as unattributable at BOTH the
post_edit emission and the submit gate, letting incomplete fixes submit unchallenged
(ipython-14798 / beets-5457 / geopandas / babel / loguru). The advisory ("run the
covering test") fired; its executed twin never did.

These tests are RED before the fix and GREEN after. The name-overlap discriminator is
mutation-guarded: dropping the overlap, the non-empty-names guard, or the
``base_verdict == "fail"`` scope each makes a listed test fail.
"""

from __future__ import annotations

from groundtruth.runtime import covering_runner as cr
from groundtruth.runtime.native_render import (
    contains_test_identity,
    render_covering_failure_native,
)


def _fake_base(monkeypatch, tmp_path) -> None:
    """Neutralize the real ``git worktree`` — the base run is driven by the executor."""
    parent = tmp_path / "parent"
    parent.mkdir()
    monkeypatch.setattr(
        cr, "_make_base_worktree",
        lambda root, executor=None: (str(tmp_path / "base"), str(parent)),
    )
    monkeypatch.setattr(
        cr, "_cleanup_base_worktree",
        lambda root, wt, parent, executor=None: None,
    )


def _executor(exit_code: int, stdout: str):
    return lambda cmd, cwd, timeout: (exit_code, stdout, "")


# The pre-existing FAIL_TO_PASS target: RED at base, STILL RED after an incomplete fix.
_BASE_RED = "tests/test_mod.py::test_target FAILED\n1 failed"


def _current_red(names):
    # An ASSERTION (value) failure — no agent-source frame, so attribution CANNOT
    # short-circuit on the trace-frame leg and MUST reach the differential.
    return {
        "verdict": "fail",
        "stdout_tail": "E   assert 3 == 5",
        "stderr_tail": "",
        "failing_test_names": list(names),
    }


def test_incomplete_fix_same_named_test_is_attributed_unresolved(monkeypatch, tmp_path):
    """base-RED + current-RED on the SAME test => attributed via unresolved_covering."""
    _fake_base(monkeypatch, tmp_path)
    result = cr.attribute_covering_red(
        _current_red(["tests/test_mod.py::test_target"]),
        {"src/mod.py"},
        test_files=["tests/test_mod.py"],
        repo_root=str(tmp_path),
        covering_files=["tests/test_mod.py"],
        executor=_executor(1, _BASE_RED),
    )
    assert result.attributed is True
    assert result.method == "unresolved_covering"
    assert result.current_verdict == "fail"
    assert result.base_verdict == "fail"
    # implicated set is the full edited set (non-empty) so lane attestation holds.
    assert result.implicated_edited_paths == ("src/mod.py",)


def test_submit_bool_wrapper_blocks_on_unresolved(monkeypatch, tmp_path):
    """The submit gate uses is_red_attributable() — it must now return True so an
    incomplete fix is BLOCKED, not waved through."""
    _fake_base(monkeypatch, tmp_path)
    assert cr.is_red_attributable(
        _current_red(["tests/test_mod.py::test_target"]),
        {"src/mod.py"},
        test_files=["tests/test_mod.py"],
        repo_root=str(tmp_path),
        covering_files=["tests/test_mod.py"],
        executor=_executor(1, _BASE_RED),
    ) is True


def test_base_red_but_different_failing_test_stays_unattributed(monkeypatch, tmp_path):
    """MUTATION GUARD (drop `& base_names` overlap): base fails test_A, current fails
    test_B — a DIFFERENT failure, not an unresolved target — must stay quiet."""
    _fake_base(monkeypatch, tmp_path)
    result = cr.attribute_covering_red(
        _current_red(["tests/test_mod.py::test_B"]),
        {"src/mod.py"},
        test_files=["tests/test_mod.py"],
        repo_root=str(tmp_path),
        covering_files=["tests/test_mod.py"],
        executor=_executor(1, "tests/test_mod.py::test_A FAILED\n1 failed"),
    )
    assert result.attributed is False
    assert result.base_verdict == "fail"


def test_base_red_unnamed_stays_unattributed(monkeypatch, tmp_path):
    """MUTATION GUARD (drop the non-empty-names guard): a base-red with NO parseable
    test names must NOT attribute — preserves the historical base-red-is-quiet
    contract (test_differential_false_result_keeps_observed_base_red)."""
    _fake_base(monkeypatch, tmp_path)
    result = cr.attribute_covering_red(
        {"verdict": "fail", "stdout_tail": "E   assert 1 == 2"},  # no failing_test_names
        {"src/mod.py"},
        repo_root=str(tmp_path),
        covering_files=["tests/test_mod.py"],
        executor=_executor(1, "1 failed"),  # base red, no nodeid
    )
    assert result.attributed is False
    assert result.method == "differential"
    assert result.base_verdict == "fail"


def test_base_unavailable_stays_unattributed(monkeypatch, tmp_path):
    """MUTATION GUARD (widen scope to base != pass): an UNAVAILABLE base (env-flaky /
    no tests collected) has no ground truth — even with a current name it must stay
    quiet (invariant ②: never block on an env artifact)."""
    _fake_base(monkeypatch, tmp_path)
    # exit 5 + 0 passed = pytest "no tests collected" => base verdict "unavailable".
    result = cr.attribute_covering_red(
        _current_red(["tests/test_mod.py::test_target"]),
        {"src/mod.py"},
        test_files=["tests/test_mod.py"],
        repo_root=str(tmp_path),
        covering_files=["tests/test_mod.py"],
        executor=_executor(5, "no tests ran"),
    )
    assert result.attributed is False
    assert result.base_verdict == "unavailable"


def test_regression_base_green_is_still_differential(monkeypatch, tmp_path):
    """Byte-behaviour on the regression path: base-GREEN -> current-RED stays the
    high-confidence `differential` attribution, unchanged."""
    _fake_base(monkeypatch, tmp_path)
    result = cr.attribute_covering_red(
        _current_red(["tests/test_mod.py::test_target"]),
        {"src/mod.py"},
        test_files=["tests/test_mod.py"],
        repo_root=str(tmp_path),
        covering_files=["tests/test_mod.py"],
        executor=_executor(0, "tests/test_mod.py::test_target PASSED\n1 passed"),
    )
    assert result.attributed is True
    assert result.method == "differential"
    assert result.base_verdict == "pass"


def test_unresolved_covering_transcript_is_leak_safe():
    """The DELIVERED bytes for an unresolved covering RED carry ZERO test identity —
    the failing-test NAME used for the host-side overlap never reaches the model."""
    cres = {
        "verdict": "fail",
        "stdout_tail": (
            "tests/test_mod.py::test_target FAILED\n"
            "src/mod.py:88: in compute\n"
            "E   assert 3 == 5\n"
            "FAILED tests/test_mod.py::test_target - assert 3 == 5"
        ),
        "stderr_tail": "",
    }
    block = render_covering_failure_native(
        cres, edited_symbol="compute", test_files=["tests/test_mod.py"])
    assert block  # a signal-bearing RED was produced
    assert "test_target" not in block
    assert contains_test_identity(block, test_files=["tests/test_mod.py"]) is False
