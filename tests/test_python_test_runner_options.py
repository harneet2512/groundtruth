import pytest

from groundtruth.runtime.patterns import TEST_RUNNER_RE, classify_test_observation


@pytest.mark.parametrize(
    "command",
    [
        "python -B -m unittest -v",
        "python3 -B -m unittest -v",
        "python3.12 -I -B -m pytest -q",
        "python3 -u -m pytest tests",
        "python3 -IB -m unittest -v",
    ],
)
def test_interpreter_options_preserve_test_protocol(command):
    assert TEST_RUNNER_RE.search(command)
    assert classify_test_observation(command, "Ran 1 test in 0.001s\n\nOK\n", 0) == (
        "pass",
        "command",
    )
    assert classify_test_observation(command, "FAILED (failures=1)\n", 1) == ("fail", "command")


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'print(\"-m pytest\")'",
        "python3 -B repair.py",
        "cat python3 -B -m unittest",
        "echo python3 -B -m unittest",
        "python3 -V -m unittest",
        "python3 -h -m unittest",
        "python3 -c 'print(1)' -m unittest",
    ],
)
def test_nonexecuted_test_spelling_does_not_establish_protocol(command):
    assert not TEST_RUNNER_RE.search(command)
