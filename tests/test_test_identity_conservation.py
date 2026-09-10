from groundtruth.runtime.test_runner import (
    _parse_failing_test_names,
    _parse_passing_test_names,
    _parse_test_output,
)
import subprocess
import sys


def test_test_identity_parsing_is_not_a_twenty_item_preview():
    passed = [f"test_module.py::test_pass_{number}" for number in range(60)]
    failed = [f"test_module.py::test_fail_{number}" for number in range(35)]
    output = "\n".join(
        [*(f"{name} PASSED" for name in passed), *(f"{name} FAILED" for name in failed)]
    )
    assert _parse_passing_test_names(output) == passed
    assert _parse_failing_test_names(output) == failed


def test_colorized_passing_identity_is_the_same_as_plain_output():
    assert _parse_passing_test_names("\x1b[32mtest_api.py::test_ok PASSED\x1b[0m\n") == [
        "test_api.py::test_ok"
    ]


def test_unittest_named_outcomes_exclude_skipped_tests():
    output = (
        "test_ok (test_api.API.test_ok) ... ok\n"
        "test_skip (test_api.API.test_skip) ... skipped 'not applicable'\n"
        "test_bad (test_api.API.test_bad) ... FAIL\n"
        "FAIL: test_bad (test_api.API.test_bad)\n"
        "Ran 3 tests in 0.001s\nFAILED (failures=1, skipped=1)\n"
    )
    assert _parse_passing_test_names(output) == ["test_api.API.test_ok"]
    assert _parse_failing_test_names(output) == ["test_api.API.test_bad"]
    assert _parse_test_output(output, ["python", "-m", "unittest", "-v"]) == {
        "passed": 1,
        "failed": 1,
        "errored": 0,
    }


def test_real_unittest_execution_keeps_all_names_and_skip_accounting(tmp_path):
    source = (
        "import unittest\nclass API(unittest.TestCase):\n"
        + "".join(
            f"    def test_{number:02d}(self): self.assertTrue(True)\n" for number in range(30)
        )
        + "    @unittest.skip('fixture exclusion')\n    def test_skip(self): self.fail()\n"
    )
    (tmp_path / "test_api.py").write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "-v"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert _parse_passing_test_names(output) == [
        f"test_api.API.test_{number:02d}" for number in range(30)
    ]
    assert _parse_failing_test_names(output) == []
    assert _parse_test_output(output, [sys.executable, "-m", "unittest", "-v"]) == {
        "passed": 30,
        "failed": 0,
        "errored": 0,
    }


def test_unittest_expected_failure_is_not_counted_as_passing():
    output = "Ran 2 tests in 0.001s\n\nOK (skipped=1, expected failures=1)\n"
    assert _parse_test_output(output, ["python", "-m", "unittest"]) == {
        "passed": 0,
        "failed": 0,
        "errored": 0,
    }
