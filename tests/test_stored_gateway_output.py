from __future__ import annotations

import hashlib
import random

import pytest

from groundtruth.runtime.adapters.miniswe import StoredOutput, normalize_event
from groundtruth.runtime.gateway import (
    GatewayState,
    _grep_hit_paths,
    _grep_hit_paths_event,
    _grep_result_empty,
    _grep_result_empty_event,
    classify_outcome,
)


def _stored(text: str, chunk_size: int = 7) -> StoredOutput:
    payload = text.encode()
    return StoredOutput(
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "utf-8",
        lambda: (
            payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)
        ),
    )


@pytest.mark.parametrize(
    ("complete", "preview", "expected"),
    [
        ("0\npkg:0\n", "1\n", "zero"),
        ("1\n", "0\n", "hit"),
        (" \t\r\n", "visible preview", "zero"),
    ],
)
def test_normalized_stored_output_drives_gateway_search_outcome(
    complete: str, preview: str, expected: str
) -> None:
    event = normalize_event("rg -c needle .", preview, 0, 4, stored_output=_stored(complete, 1))
    state = GatewayState()

    classify_outcome(event, state)

    assert state.ledger["needle"]["outcomes"] == [expected]
    assert event.output == preview


def test_stored_grep_helpers_match_legacy_complete_text() -> None:
    generator = random.Random(812)
    lines = (
        "",
        " ",
        "0",
        "pkg:0",
        "pkg:1",
        "src/app.py:12:value",
        "./tests/test_app.py:7:hit",
        "not a path:hit",
        "README.md:hit",
    )
    commands = ("rg needle .", "rg -c needle .", "grep --count needle .")
    for _ in range(500):
        output = "\n".join(generator.choice(lines) for _ in range(generator.randrange(0, 30)))
        command = generator.choice(commands)
        event = normalize_event(
            command,
            "[preview]",
            0,
            1,
            test_outcome="unobserved",
            stored_output=_stored(output, generator.randrange(1, 12)),
        )
        assert _grep_result_empty_event(event) == _grep_result_empty(command, output)
        assert _grep_hit_paths_event(event, ".") == _grep_hit_paths(output, ".")


def test_long_irrelevant_line_does_not_hide_late_unique_hit_path() -> None:
    output = "x" * 200_000 + "\nsrc/unique.py:731:def target\n"
    event = normalize_event(
        "rg target .",
        "[preview]",
        0,
        1,
        test_outcome="unobserved",
        stored_output=_stored(output, 509),
    )

    assert _grep_hit_paths_event(event, ".") == _grep_hit_paths(output, ".")
