"""B1 (verification-execution) — Stage-1 deterministic tests.

Covers the three pure engines (covering_runner, native_render, submit_gate) plus
the harvested-parser generalization (test_runner._detect_runner). Asserts the plan
§6 invariants that make "the gate fires" into "the gate is correct":
- accuracy ②: a covering RED requires POSITIVE failure evidence (conservative).
- native-delivery ⑤: ZERO ``<gt-*>`` on any model-facing render.
- determinism ⑧: same input -> same bytes.
- leak-safe ⑦: the renderer only ever shows command + runner stdout/stderr.

Plus a REAL Python subprocess execution (not mocked) so the wrapper is proven by
running it, not by reasoning about it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from groundtruth.runtime import covering_runner as cr
from groundtruth.runtime import native_render as nr
from groundtruth.runtime import submit_gate as sg
from groundtruth.runtime.test_runner import _detect_runner


# ---------------------------------------------------------------------------
# test_runner._detect_runner — runner recognized across wrapped/prefixed tokens
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command,expected",
    [
        (["pytest", "tests/x.py"], "pytest"),
        (["python", "-m", "pytest", "tests/x.py"], "pytest"),
        (["tox"], "pytest"),
        (["go", "test", "./..."], "go"),
        (["timeout", "60", "cargo", "test"], "cargo"),
        (["npx", "jest", "a.test.js"], "jsnode"),
        (["node", "--test"], "jsnode"),
        (["/usr/bin/pytest", "x"], "pytest"),
        ([], ""),
    ],
)
def test_detect_runner(command, expected):
    assert _detect_runner(command) == expected


def test_detect_runner_compiled_wins_over_node():
    # a stray "node.go" file arg must not flip a go run to jsnode
    assert _detect_runner(["go", "test", "node.go"]) == "go"


# ---------------------------------------------------------------------------
# covering_runner.build_covering_command — file-scoped, name-less, per-language
# ---------------------------------------------------------------------------
def test_build_command_per_language():
    assert cr.build_covering_command("pkg/foo_test.py") == ["pytest", "pkg/foo_test.py"]
    assert cr.build_covering_command("pkg/foo_test.go") == ["go", "test", "./pkg"]
    assert cr.build_covering_command("src/lib_test.rs") == ["cargo", "test", "lib_test"]
    assert cr.build_covering_command("a/b.test.ts") == ["npx", "jest", "a/b.test.ts"]
    assert cr.build_covering_command("FooTest.java") == ["mvn", "test", "-Dtest=FooTest"]


def test_build_command_unknown_ext_is_none():
    assert cr.build_covering_command("README.md") is None
    assert cr.build_covering_command("") is None


def test_build_command_never_contains_a_test_name():
    # leak-safety: the command is FILE-scoped; no ::name, no -k, no -run selector.
    for f in ("t_test.py", "t_test.go", "t.test.ts", "t_test.rs"):
        cmd = cr.build_covering_command(f)
        joined = " ".join(cmd or [])
        assert "::" not in joined
        assert " -k " not in joined
        assert "-run" not in joined


# ---------------------------------------------------------------------------
# covering_runner._classify_one — CONSERVATIVE (accuracy invariant ②)
# ---------------------------------------------------------------------------
def _exec(**over):
    base = dict(
        executed=True, timed_out=False, environment_failure_class=None,
        exit_code=0, passed=0, failed=0, errored=0, failing_test_names=[],
    )
    base.update(over)
    return base


def test_classify_fail_requires_positive_evidence():
    assert cr._classify_one(_exec(exit_code=1, failed=1)) == "fail"
    assert cr._classify_one(_exec(exit_code=1, failing_test_names=["t"])) == "fail"
    assert cr._classify_one(_exec(exit_code=1, errored=1)) == "fail"


def test_classify_ambiguous_nonzero_is_unavailable_not_fail():
    # non-zero exit but NO parsed failure evidence -> unavailable, never a block.
    assert cr._classify_one(_exec(exit_code=1, passed=0, failed=0)) == "unavailable"


def test_classify_green_exit_with_failure_prose_is_not_fail():
    # Fable F1: a GREEN run (exit 0) whose output merely CONTAINS "FAILED" / "N failed"
    # (a pytest warning summary, a jest console.log) must NOT be a fabricated RED.
    assert cr._classify_one(_exec(exit_code=0, failing_test_names=["handshake"])) == "unavailable"
    assert cr._classify_one(_exec(exit_code=0, errored=1, passed=0)) == "unavailable"
    # exit 0 with real passes AND stray "failed" prose -> a pass, not a block.
    assert cr._classify_one(_exec(exit_code=0, failed=2, passed=5)) == "pass"


def test_classify_pytest_no_tests_is_unavailable():
    assert cr._classify_one(_exec(exit_code=5, passed=0)) == "unavailable"


def test_classify_pass_requires_exit0_and_positive_passes():
    assert cr._classify_one(_exec(exit_code=0, passed=3)) == "pass"
    assert cr._classify_one(_exec(exit_code=0, passed=0)) == "unavailable"  # empty run


def test_classify_env_and_timeout_are_unavailable():
    assert cr._classify_one(_exec(environment_failure_class="offline_install_or_proxy",
                                  exit_code=1, failed=2)) == "unavailable"
    assert cr._classify_one(_exec(timed_out=True, exit_code=None)) == "unavailable"
    assert cr._classify_one(_exec(executed=False, exit_code=None)) == "unavailable"


# ---------------------------------------------------------------------------
# covering_runner.run_covering_tests — aggregation (execute_test_command mocked)
# ---------------------------------------------------------------------------
def _mock_exec(monkeypatch, table):
    """table: {test_file_basename_substring: result_dict}. duration defaults 10ms."""
    def fake(repo_root, command, **kw):
        key = " ".join(command)
        for sub, res in table.items():
            if sub in key:
                out = dict(res)
                out.setdefault("duration_ms", 10)
                out.setdefault("command", command)
                return out
        return _exec(executed=False, exit_code=None, duration_ms=1, command=command)
    monkeypatch.setattr(cr, "execute_test_command", fake)


def test_aggregate_fail_wins(monkeypatch):
    _mock_exec(monkeypatch, {
        "pass_test.py": _exec(exit_code=0, passed=2),
        "fail_test.py": _exec(exit_code=1, failed=1, failing_test_names=["t_x"]),
    })
    r = cr.run_covering_tests("/repo", ["a/pass_test.py", "a/fail_test.py"])
    assert r["verdict"] == "fail"
    assert r["failing_test_names"] == ["t_x"]


def test_aggregate_pass_when_no_failure(monkeypatch):
    _mock_exec(monkeypatch, {"pass_test.py": _exec(exit_code=0, passed=2)})
    r = cr.run_covering_tests("/repo", ["a/pass_test.py"])
    assert r["verdict"] == "pass"


def test_aggregate_unavailable_when_nothing_conclusive(monkeypatch):
    _mock_exec(monkeypatch, {"x_test.py": _exec(exit_code=5, passed=0)})
    r = cr.run_covering_tests("/repo", ["a/x_test.py"])
    assert r["verdict"] == "unavailable"


def test_aggregate_no_files_is_unavailable():
    r = cr.run_covering_tests("/repo", [])
    assert r["verdict"] == "unavailable"
    assert r["executed"] is False


def test_pass_preferred_over_earlier_unavailable(monkeypatch):
    _mock_exec(monkeypatch, {
        "u_test.py": _exec(exit_code=5, passed=0),
        "p_test.py": _exec(exit_code=0, passed=1),
    })
    r = cr.run_covering_tests("/repo", ["a/u_test.py", "a/p_test.py"])
    assert r["verdict"] == "pass"


def test_executor_rc_none_before_spawn_is_not_executed():
    def rejected(_cmd, _cwd, _timeout):
        return None, "", "replay_verification_unsupported_command"

    result = cr._run_via_executor(
        rejected, "/repo", ["pytest", "a/test_mod.py"],
        timeout=10, max_output_chars=4000,
    )

    assert result["executed"] is False
    assert result["timed_out"] is False


def test_executor_timeout_preserves_started_and_timed_out_truth():
    def timed_out(_cmd, _cwd, _timeout):
        return None, "", "replay_verification_timeout"

    result = cr._run_via_executor(
        timed_out, "/repo", ["pytest", "a/test_mod.py"],
        timeout=10, max_output_chars=4000,
    )

    assert result["executed"] is True
    assert result["timed_out"] is True
    assert result["reason"] == "timeout"


# ---------------------------------------------------------------------------
# native_render — Format D: anonymized native failure block
#   KEEP error-class + native value-comparison + AGENT-own-source frames.
#   STRIP command line, nodeid, FAILURES header, def echo, test-file frames,
#   summary. ZERO <gt-*>, ZERO test identity. Deterministic.
# ---------------------------------------------------------------------------
_PYTEST_CRASH = (
    "=================================== FAILURES ===================================\n"
    "_________________________________ test_refund _________________________________\n"
    "tests/test_pay.py:12: in test_refund\n"
    "    result = transform(5)\n"
    "src/mymodule.py:88: in transform\n"
    "    return a + b\n"
    "E   TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
    "FAILED tests/test_pay.py::test_refund - TypeError"
)


def test_format_d_keeps_signal_strips_identity_pytest():
    r = _exec(exit_code=1, failed=1, command=["pytest", "tests/test_pay.py"],
              stdout_tail=_PYTEST_CRASH, stderr_tail="")
    out = nr.render_covering_failure_native(r, edited_symbol="transform",
                                            test_files=["tests/test_pay.py"])
    # signal kept: error class + the agent's OWN source frame
    assert "TypeError" in out and "src/mymodule.py:88" in out and "transform" in out
    # identity stripped: no test name, no test file, no nodeid
    assert "test_refund" not in out and "test_pay" not in out and "::" not in out
    assert not nr.contains_gt_tag(out)
    assert not nr.contains_test_identity(out, ["tests/test_pay.py"])


@pytest.mark.parametrize("runner,stdout,tf,keep,ban", [
    ("go", "--- FAIL: TestRefund (0.0s)\n    pay_test.go:14: got 7 want 42",
     ["pay_test.go"], "want 42", "TestRefund"),
    ("cargo", "thread 'm::tests::refund' panicked at src/lib.rs:9:5:\nassertion `left == right` failed\n  left: 7\n  right: 42",
     [], "left: 7", "tests::refund"),
    ("jest", "  ● Suite › the amount\n    Expected: 42\n    Received: 7\n      at tests/pay.test.js:3:1",
     ["tests/pay.test.js"], "Expected: 42", "pay.test.js"),
])
def test_format_d_leak_safe_across_runners(runner, stdout, tf, keep, ban):
    r = _exec(exit_code=1, failed=1, command=[runner], stdout_tail=stdout, stderr_tail="")
    out = nr.render_covering_failure_native(r, edited_symbol="fn", test_files=tf)
    assert keep in out, (runner, out)
    assert ban not in out, (runner, out)
    assert not nr.contains_test_identity(out, tf), (runner, out)
    assert not nr.contains_gt_tag(out)


def test_format_d_empty_is_blank_and_deterministic():
    assert nr.render_covering_failure_native({}) == ""
    assert nr.render_covering_failure_native(_exec(command=[], stdout_tail="", stderr_tail="")) == ""
    r = _exec(exit_code=1, stdout_tail=_PYTEST_CRASH, stderr_tail="")
    assert (nr.render_covering_failure_native(r, edited_symbol="transform")
            == nr.render_covering_failure_native(r, edited_symbol="transform"))


def test_format_d_frames_line_names_agent_symbol_only():
    r = _exec(exit_code=1, stdout_tail=_PYTEST_CRASH, stderr_tail="")
    out = nr.render_covering_failure_native(r, edited_symbol="transform",
                                            test_files=["tests/test_pay.py"])
    assert out.splitlines()[0] == "Your change to `transform()` fails a covering test:"


# ---------------------------------------------------------------------------
# attribution gate — deliver ONLY a red the edit plausibly caused
# ---------------------------------------------------------------------------
def test_attribution_true_when_deepest_frame_in_edited_file():
    r = _exec(exit_code=1, stdout_tail=_PYTEST_CRASH, stderr_tail="")
    assert nr.is_edit_attributed(r, ["src/mymodule.py"], test_files=["tests/test_pay.py"]) is True


def test_attribution_false_when_edit_elsewhere_or_no_agent_frame():
    r = _exec(exit_code=1, stdout_tail=_PYTEST_CRASH, stderr_tail="")
    assert nr.is_edit_attributed(r, ["src/other.py"], test_files=["tests/test_pay.py"]) is False
    # a red whose only frames are the test file (no agent frame) is NOT attributed
    r2 = _exec(exit_code=1, stdout_tail="tests/test_pay.py:1: in test_x\nE   AssertionError", stderr_tail="")
    assert nr.is_edit_attributed(r2, ["src/mymodule.py"], test_files=["tests/test_pay.py"]) is False


def test_render_submit_rejection_is_precommit_shape_no_gt_tag():
    out = nr.render_submit_rejection("a covering test is failing", "generic detail")
    assert "pre-commit" in out.lower()
    assert "exit 1" in out
    assert not nr.contains_gt_tag(out)


# ---------------------------------------------------------------------------
# submit_gate.gate_verdict — fail law
# ---------------------------------------------------------------------------
def test_gate_allows_clean():
    assert sg.gate_verdict(covering={"verdict": "pass"}).allow is True
    assert sg.gate_verdict(covering={"verdict": "unavailable"}).allow is True
    assert sg.gate_verdict(covering=None, hygiene=None).allow is True


def test_gate_blocks_covering_fail():
    v = sg.gate_verdict(covering={"verdict": "fail", "failing_test_names": ["t_x"]})
    assert v.allow is False
    assert v.reason == "covering_test_failed"
    # F7b: the model-facing detail is generic — NO test names on the rejection surface.
    assert "t_x" not in v.detail
    assert v.record["covering_failing_names"] == ["t_x"]  # names kept host-side only


def test_gate_blocks_hygiene():
    v = sg.gate_verdict(hygiene={"blocking": True, "reason": "binary_in_diff"})
    assert v.allow is False
    assert v.reason == "hygiene"


def test_gate_fails_open_after_max_bounces():
    v = sg.gate_verdict(covering={"verdict": "fail"}, bounce_count=1, max_bounces=1)
    assert v.allow is True
    assert v.reason == "gate_overridden"


def test_safe_gate_verdict_never_raises():
    # a covering value that explodes on attribute access -> ALLOW + gate_crash.
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("kaboom")
    v = sg.safe_gate_verdict(covering=Boom())
    assert v.allow is True
    assert v.reason == "gate_crash"


# ---------------------------------------------------------------------------
# REAL Python subprocess execution — prove the wrapper by running it
# ---------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("pytest") is None, reason="pytest not on PATH")
def test_real_python_covering_fail_then_pass(tmp_path: Path):
    repo = tmp_path
    (repo / "fail_test.py").write_text(textwrap.dedent("""
        def test_fails():
            assert 1 == 2
    """))
    (repo / "pass_test.py").write_text(textwrap.dedent("""
        def test_passes():
            assert 1 == 1
    """))
    # a real failing covering file -> verdict "fail" with real runner output.
    r_fail = cr.run_covering_tests(str(repo), ["fail_test.py"])
    assert r_fail["verdict"] == "fail", r_fail
    rendered = nr.render_covering_failure_native(r_fail, edited_symbol="thing",
                                                 test_files=["fail_test.py"])
    # Format D on a real pytest RED: the assertion delta survives, the test file
    # name + nodeid do not.
    assert "1 == 2" in rendered
    assert "fail_test.py" not in rendered and "test_fails" not in rendered
    assert not nr.contains_gt_tag(rendered)
    assert not nr.contains_test_identity(rendered, ["fail_test.py"])
    # a real passing covering file -> verdict "pass".
    r_pass = cr.run_covering_tests(str(repo), ["pass_test.py"])
    assert r_pass["verdict"] == "pass", r_pass
    # the gate blocks the fail, allows the pass.
    assert sg.gate_verdict(covering=r_fail).allow is False
    assert sg.gate_verdict(covering=r_pass).allow is True


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
