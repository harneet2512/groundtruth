from __future__ import annotations

import hashlib
import random
import tracemalloc

import pytest

from groundtruth.runtime.adapters.miniswe import (
    StoredOutput,
    StoredToolEvent,
    canonical_test_failure_fingerprint,
    normalize_event,
)


def _stored(payload: bytes, chunk_size: int = 17) -> StoredOutput:
    try:
        payload.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = "base64"
    return StoredOutput(
        sha256=hashlib.sha256(payload).hexdigest(),
        total_length=len(payload),
        encoding=encoding,
        open_bytes=lambda: (
            payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)
        ),
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 127])
def test_line_fragments_preserve_python_splitlines(chunk_size: int) -> None:
    text = "first\r\n\rsecond\vthird\ffourth\x1cfifth\x85sixth\u2028last\n"
    source = _stored(text.encode(), chunk_size)
    lines: list[str] = []
    current = ""
    for fragment, line_end in source.iter_line_fragments():
        current += fragment
        if line_end:
            lines.append(current)
            current = ""

    assert lines == text.splitlines()


def test_normalization_classifies_complete_stored_output_not_preview() -> None:
    payload = b"25 passed\n" + b"x" * 70_000 + b"\n1 failed\n"
    event = normalize_event(
        "pytest",
        "25 passed\n[preview only]",
        1,
        3,
        stored_output=_stored(payload, 509),
    )

    assert isinstance(event, StoredToolEvent)
    assert event.output == "25 passed\n[preview only]"
    assert (event.test_outcome, event.test_protocol) == ("fail", "command")
    assert event.stored_output.identity() == {
        "schema": "gt.output_artifact.v1",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "total_length": len(payload),
        "encoding": "utf-8",
    }


def test_stored_failure_fingerprint_matches_full_output_across_giant_lines() -> None:
    lines = [
        f"FAILED case {index} at src/pkg/mod_{index}.py:0x{index + 16:x}" for index in range(12)
    ]
    lines.insert(5, "\x1b[31mAssertionError\x1b[0m " + "x" * 1_000 + " C:\\repo\\test.py:731")
    output = "\r\n".join(lines) + "\r\nignored success"
    expected = canonical_test_failure_fingerprint(
        normalize_event("pytest", output, 1, 1, test_outcome="fail")
    )
    stored_event = normalize_event(
        "pytest",
        "[preview]",
        1,
        1,
        test_outcome="fail",
        stored_output=_stored(output.encode(), 13),
    )

    assert canonical_test_failure_fingerprint(stored_event) == expected


def test_stored_fingerprint_matches_frozen_transform_for_random_fragments() -> None:
    generator = random.Random(731)
    atoms = (
        "error",
        "FAILED",
        "failure",
        "AssertionError",
        "panic",
        "safe",
        "  ",
        "\t",
        "\n",
        "\r\n",
        "0xdeadbeef",
        "0Xdeadbeef",
        "731",
        "src/pkg/mod.py",
        "C:\\repo\\test.py",
        "\x1b[31m",
        "\x1b[0m",
        "Ok",
    )
    for _ in range(500):
        output = "".join(generator.choice(atoms) for _ in range(generator.randrange(1, 60)))
        full = normalize_event("pytest", output, 1, 1, test_outcome="fail")
        stored = normalize_event(
            "pytest",
            "[preview]",
            1,
            1,
            test_outcome="fail",
            stored_output=_stored(output.encode(), generator.randrange(1, 24)),
        )
        assert canonical_test_failure_fingerprint(stored) == (
            canonical_test_failure_fingerprint(full)
        )


def test_stored_output_integrity_failure_is_not_normalized_or_fingerprinted(
    monkeypatch,
) -> None:
    import groundtruth.runtime.patterns as patterns

    payload = b"1 failed\n"
    source = StoredOutput(
        sha256="0" * 64,
        total_length=len(payload),
        encoding="utf-8",
        open_bytes=lambda: (payload,),
    )
    with pytest.raises(ValueError, match="stored output digest mismatch"):
        normalize_event("pytest", "[preview]", 1, 1, stored_output=source)

    def early_result(command, chunks, returncode):
        next(iter(chunks))
        return "fail", "command"

    monkeypatch.setattr(patterns, "classify_test_observation_stream", early_result)
    with pytest.raises(ValueError, match="stored output digest mismatch"):
        normalize_event("pytest", "[preview]", 1, 1, stored_output=source)

    event = normalize_event("pytest", "[preview]", 1, 1, test_outcome="fail", stored_output=source)
    with pytest.raises(ValueError, match="stored output digest mismatch"):
        canonical_test_failure_fingerprint(event)


def test_binary_stored_fingerprint_preserves_utf8_replacement_semantics() -> None:
    payload = b"AssertionError \xff at src/pkg/test.py:731\n"
    expected = canonical_test_failure_fingerprint(
        normalize_event("pytest", payload.decode("utf-8", "replace"), 1, 1, test_outcome="fail")
    )
    event = normalize_event(
        "pytest",
        "[base64 preview]",
        1,
        1,
        test_outcome="fail",
        stored_output=_stored(payload, 1),
    )

    assert event.stored_output.encoding == "base64"
    assert canonical_test_failure_fingerprint(event) == expected


# Deliberately heavy: this feeds megabytes through the classifier to prove the
# bound holds at scale, so it costs tens of seconds by design. The suite-wide
# --timeout=60 is sized for ordinary tests; declaring this one's real budget
# keeps the assertion intact instead of shrinking the input it exists to test.
@pytest.mark.timeout(300)
def test_giant_significant_line_has_bounded_fingerprint_memory() -> None:
    payload = b"AssertionError " + b"x" * 3_000_000 + b" src/pkg/test.py:731\n"
    normalized = b"AssertionError " + b"x" * 3_000_000 + b" :"
    expected = hashlib.sha256(normalized).hexdigest()[:16]
    event = normalize_event(
        "pytest",
        "[preview]",
        1,
        1,
        test_outcome="fail",
        stored_output=_stored(payload, 8192),
    )

    tracemalloc.start()
    actual = canonical_test_failure_fingerprint(event)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert actual == expected
    assert peak < 512 * 1024
