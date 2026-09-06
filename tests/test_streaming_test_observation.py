from __future__ import annotations

import json
import subprocess
import sys

import pytest

from groundtruth.runtime.patterns import (
    StreamingTestClassifier,
    classify_test_observation,
    classify_test_observation_stream,
)


@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    [
        ("python3 -B -m unittest -v", "Ran 1 test in 0.001s\n\nOK\n", 0),
        ("python3.12 -I -B -m pytest -q", "25 passed in 1.00s\n", 0),
        ("pytest -q", "25 passed\n" + "progress\n" * 2_000 + "1 failed\n", 0),
        ("pytest -q", "1 failed\n" + "progress\n" * 2_000 + "25 passed\n", 0),
        ("pytest -q", "ModuleNotFoundError: No module named x\n", 1),
        ("pytest -q", "collected 0 items\n", 0),
        ("pytest -q", "1 PASSING\n", 1),
        ("pytest -q", "Ok package 1.0s\n", 0),
        ("pytest -q", "failures: 1\n", 1),
        ("./target/debug/deps/check", "running 1 test\ntest result: ok. 1 passed\n", 0),
        ("./target/debug/deps/check", "running 1 test\ntest result: FAILED. 1 failed\n", 1),
        ("echo pytest", "FAILED\n", 1),
    ],
)
@pytest.mark.parametrize("chunk_size", [1, 7, 127, 8192])
def test_streaming_classifier_matches_complete_canonical_result(
    command: str, output: str, returncode: int, chunk_size: int
) -> None:
    chunks = (
        output[index:index + chunk_size]
        for index in range(0, len(output), chunk_size)
    )
    assert classify_test_observation_stream(command, chunks, returncode) == (
        classify_test_observation(command, output, returncode)
    )


def test_stream_accepts_utf8_bytes_split_inside_codepoint() -> None:
    output = ("progress café\n" * 200 + "17 passed\n").encode()
    chunks = (output[index:index + 3] for index in range(0, len(output), 3))

    assert classify_test_observation_stream("pytest", chunks, 0) == (
        "pass", "command"
    )


def test_failure_beyond_transport_preview_keeps_precedence() -> None:
    classifier = StreamingTestClassifier("pytest")
    classifier.feed("25 passed\n")
    for _ in range(20_000):
        classifier.feed("progress that is absent from the transport preview\n")
    classifier.feed("1 failed\n")

    assert classifier.finish(0) == ("fail", "command")


def test_giant_single_line_preserves_late_markers_and_unbounded_env_form() -> None:
    passing = StreamingTestClassifier("pytest")
    environmental = StreamingTestClassifier("pytest")
    environmental.feed("error: command ")
    for _ in range(1_024):
        block = "x" * 8_192
        passing.feed(block)
        environmental.feed(block)
    passing.feed(" 41 passed\n")
    environmental.feed(" failed\n")

    assert passing.finish(0) == ("pass", "command")
    assert environmental.finish(1) == ("env_fail", "command")


def test_unbounded_numeric_and_whitespace_forms_cross_every_internal_buffer() -> None:
    cases = [
        ("pytest", "Failures:" + " " * 70_000 + "1\n", ("fail", "command")),
        ("./check", "running" + " " * 70_000 + "1 tests\n", ("", "native")),
        ("pytest", "1" + "0" * 70_000 + " failed\n", ("fail", "command")),
    ]
    for command, output, expected in cases:
        chunks = (output[index:index + 257] for index in range(0, len(output), 257))
        assert classify_test_observation_stream(command, chunks, 1) == expected
        assert expected == classify_test_observation(command, output, 1)


@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    [
        ("./check", "test result:" + " " * 70_000 + "ok\n", 0),
        ("pytest", "OK" + " " * 70_000 + "(1 test)\n", 0),
        ("pytest", "OK (1" + "0" * 70_000 + " tests)\n", 0),
        ("pytest", "Tests:" + " " * 70_000 + "1 passed\n", 0),
        ("pytest", "collected" + " " * 70_000 + "0 items\n", 0),
        ("./check", " " * 70_000 + "running 0 tests\n", 0),
        ("pytest", "OK" + " " * 70_000 + "(0 tests)\n", 0),
    ],
    ids=[
        "native-result",
        "unittest-pass",
        "unittest-long-count",
        "tests-summary",
        "collected-zero",
        "native-zero",
        "unittest-zero",
    ],
)
def test_all_unbounded_formal_markers_match_complete_classifier(
    command: str, output: str, returncode: int
) -> None:
    chunks = (output[index:index + 509] for index in range(0, len(output), 509))
    assert classify_test_observation_stream(command, chunks, returncode) == (
        classify_test_observation(command, output, returncode)
    )


@pytest.mark.parametrize(
    ("output", "returncode"),
    [
        ("100000000000000000000 passed", 0),
        ("100000000000000000000 passing", 0),
        ("Tests:" + " " * 70_000 + "100000 passed", 0),
        ("100000000000000000000 failed", 1),
        ("100000000000000000000 failing", 1),
        ("Tests:" + " " * 70_000 + "100000 failed", 1),
        ("Failures:" + "\t " * 35_000 + "100000", 1),
        ("Errors:" + "\n\t" * 35_000 + "100000", 1),
        ("no" + "\t " * 35_000 + "tests" + " " * 70_000 + "ran", 0),
        ("ran" + " " * 70_000 + "0" + "\t" * 70_000 + "tests", 0),
        ("0" + " " * 70_000 + "passing", 0),
        ("Tests run:" + " " * 70_000 + "0", 0),
        ("No tests were" + " " * 70_000 + "found", 0),
        ("Tests:" + " " * 70_000 + "0" + " " * 70_000 + "total", 0),
        (
            "Tests:"
            + " " * 70_000
            + "0"
            + " " * 70_000
            + "passed,"
            + " " * 70_000
            + "0"
            + " " * 70_000
            + "total",
            0,
        ),
        ("ok" + " " * 70_000 + "x" * 70_000 + " " * 70_000 + "1.0s", 0),
        ("AttributeError: module '" + "x" * 70_000 + "' has no attribute", 1),
        ("ld returned " + "1" * 70_000 + " exit status", 1),
        ("Interrupted: " + "1" * 70_000 + " error", 1),
    ],
    ids=[
        "passed-digits", "passing-digits", "tests-passed", "failed-digits",
        "failing-digits", "tests-failed", "failures-mixed-space",
        "errors-newlines", "no-tests-ran", "ran-zero", "zero-passing",
        "tests-run-zero", "no-tests-found", "tests-zero-total",
        "tests-zero-passed-total", "unittest-duration-token",
        "attribute-module", "linker-digits", "interrupted-digits",
    ],
)
def test_every_unbounded_repeat_in_formal_patterns_has_streaming_parity(
    output: str, returncode: int
) -> None:
    expected = classify_test_observation("pytest", output, returncode)
    chunks = (output[index:index + 509] for index in range(0, len(output), 509))
    assert classify_test_observation_stream("pytest", chunks, returncode) == expected


def test_failure_precedence_is_independent_of_long_environment_marker_order() -> None:
    output = "1 failed\nerror: command " + "x" * 70_000 + " failed\n"
    assert classify_test_observation_stream(
        "pytest", (output[index:index + 509] for index in range(0, len(output), 509)), 1
    ) == classify_test_observation("pytest", output, 1) == ("fail", "command")

    carriage_return = "error: command " + "x" * 70_000 + "\r failed"
    assert classify_test_observation_stream(
        "pytest",
        (
            carriage_return[index:index + 509]
            for index in range(0, len(carriage_return), 509)
        ),
        1,
    ) == classify_test_observation("pytest", carriage_return, 1) == (
        "env_fail",
        "command",
    )


def test_artificial_window_boundary_cannot_create_word_boundary() -> None:
    output = "x" * 70_000 + "FAILED suffix\n"
    assert classify_test_observation("pytest", output, 1) == ("", "command")
    assert classify_test_observation_stream(
        "pytest", (output[index:index + 8_192] for index in range(0, len(output), 8_192)), 1
    ) == ("", "command")


@pytest.mark.parametrize(
    "output",
    [
        "\x1b[31m1 failed\x1b[0m\n",
        "\x1b[32m1 passed\x1b[0m\n",
        "\x1b[33mcollected 0 items\x1b[0m\n",
        "\x1b[31mModuleNotFoundError\x1b[0m\n",
        "running 1 test\ntest result: FAILED\n",
    ],
)
def test_ansi_and_marker_splits_match_frozen_classifier(output: str) -> None:
    expected = classify_test_observation("pytest", output, 1)
    encoded = output.encode()
    for split in range(len(encoded) + 1):
        assert classify_test_observation_stream(
            "pytest", (encoded[:split], encoded[split:]), 1
        ) == expected


def test_streaming_peak_memory_is_independent_of_complete_output_size() -> None:
    script = r'''
import ctypes
import json
import os
import sys
import tracemalloc
from groundtruth.runtime.patterns import classify_test_observation_stream

def process_peak_bytes():
    if os.name != "nt":
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == "darwin" else value * 1024
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    return counters.PeakWorkingSetSize

def chunks():
    block = b"x" * 8192
    for _ in range(1024):
        yield block
    yield b"\n31 passed in 2.0s\n"

baseline_rss = process_peak_bytes()
tracemalloc.start()
result = classify_test_observation_stream("python3 -B -m pytest -q", chunks(), 0)
_, peak = tracemalloc.get_traced_memory()
print(json.dumps({
    "result": result, "tracemalloc_peak": peak,
    "rss_growth": process_peak_bytes() - baseline_rss,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    proof = json.loads(completed.stdout)

    assert proof["result"] == ["pass", "command"]
    assert proof["tracemalloc_peak"] < 2 * 1024 * 1024
    assert proof["rss_growth"] < 4 * 1024 * 1024
