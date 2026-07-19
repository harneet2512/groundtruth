"""RED-first contracts for source-state-bound failure identity."""
from __future__ import annotations

from groundtruth.runtime.ack_failure_identity import (
    build_syntax_failure_identity,
    implicated_source_path,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def test_cross_producer_syntax_diagnostics_have_one_exact_identity() -> None:
    edit_diagnostic = '''File "src/parser.py", line 7
    return (
            ^
SyntaxError: '(' was never closed'''
    covering_diagnostic = '''src/parser.py:7: SyntaxError: '(' was never closed'''

    edit = build_syntax_failure_identity("src/parser.py", SHA_A, edit_diagnostic)
    covering = build_syntax_failure_identity(
        "src/parser.py", SHA_A, covering_diagnostic
    )

    assert edit is not None
    assert edit == covering


def test_identity_is_fail_closed_when_any_required_component_is_missing() -> None:
    diagnostic = "src/parser.py:7: SyntaxError: invalid syntax"

    assert build_syntax_failure_identity("", SHA_A, diagnostic) is None
    assert build_syntax_failure_identity("src/parser.py", "", diagnostic) is None
    assert build_syntax_failure_identity("src/parser.py", SHA_A, "SyntaxError: invalid syntax") is None
    assert build_syntax_failure_identity("src/parser.py", SHA_A, "src/parser.py:7: failure") is None


def test_file_state_location_and_message_are_all_identity_dimensions() -> None:
    base = build_syntax_failure_identity(
        "src/parser.py", SHA_A,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )

    assert base is not None
    assert base != build_syntax_failure_identity(
        "src/parser.py", SHA_B,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )
    assert base != build_syntax_failure_identity(
        "src/other.py", SHA_A,
        "src/other.py:7: SyntaxError: invalid syntax",
    )
    assert base != build_syntax_failure_identity(
        "src/parser.py", SHA_A,
        "src/parser.py:8: SyntaxError: invalid syntax",
    )
    assert base != build_syntax_failure_identity(
        "src/parser.py", SHA_A,
        "src/parser.py:7: SyntaxError: unexpected indent",
    )


def test_implicated_source_requires_one_exact_repo_relative_location() -> None:
    diagnostic = "src/parser.py:7: SyntaxError: invalid syntax"

    assert implicated_source_path(
        diagnostic, {"src/parser.py", "src/other.py"}
    ) == "src/parser.py"
    assert implicated_source_path(
        "parser.py:7: SyntaxError: invalid syntax",
        {"src/parser.py"},
    ) is None
    assert implicated_source_path(
        "src/a.py:1: SyntaxError: bad\nsrc/b.py:2: SyntaxError: bad",
        {"src/a.py", "src/b.py"},
    ) is None


def test_exact_configured_root_normalizes_absolute_container_diagnostic() -> None:
    relative = build_syntax_failure_identity(
        "src/parser.py", SHA_A,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )
    rooted = build_syntax_failure_identity(
        "src/parser.py", SHA_A,
        "/testbed/src/parser.py:7: SyntaxError: invalid syntax",
        repo_root="/testbed",
    )

    assert rooted == relative
    assert implicated_source_path(
        "/testbed/src/parser.py:7: SyntaxError: invalid syntax",
        {"src/parser.py"},
        repo_root="/testbed",
    ) == "src/parser.py"
    assert implicated_source_path(
        "/other/src/parser.py:7: SyntaxError: invalid syntax",
        {"src/parser.py"},
        repo_root="/testbed",
    ) is None


def test_colorized_diagnostic_yields_same_identity_as_plain() -> None:
    """ANSI/CSI-wrapped toolchain output must produce byte-identical identities.

    Colorized pytest/CPython output wraps the path and error tokens in CSI
    sequences; every anchored recognizer here would otherwise capture the
    escape bytes into the path (never matching a candidate) or miss the error
    line outright — silently disabling ack suppression under colorized runners.
    """
    plain = "src/parser.py:7: SyntaxError: invalid syntax"
    colorized = (
        "\x1b[1m\x1b[31msrc/parser.py\x1b[0m:7: "
        "\x1b[1mSyntaxError\x1b[0m: invalid syntax"
    )

    assert implicated_source_path(
        colorized, {"src/parser.py", "src/other.py"}
    ) == "src/parser.py"

    plain_identity = build_syntax_failure_identity("src/parser.py", SHA_A, plain)
    color_identity = build_syntax_failure_identity("src/parser.py", SHA_A, colorized)
    assert plain_identity is not None
    assert color_identity == plain_identity
