"""Behavioral seam contracts for acknowledged cross-producer failure dedup."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.ack_failure_identity import (  # noqa: E402
    build_syntax_failure_identity,
)


def _reset(monkeypatch) -> None:
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_SS_ACK_METRICS", "1")
    g._ss_reset()


def test_duplicate_is_eligible_until_exact_delivery_is_acknowledged(monkeypatch) -> None:
    _reset(monkeypatch)
    identity = build_syntax_failure_identity(
        "src/parser.py", "a" * 64,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )
    assert identity is not None
    syntax = 'File "src/parser.py", line 7\nSyntaxError: invalid syntax'
    covering = "A covering test fails:\n  at src/parser.py:7\nSyntaxError: invalid syntax"

    assert not g._ss_ack_failure_suppresses("edit.syntax", syntax, identity)
    g._action_count = 3
    g._ss_note_delivery_for_ack("edit.syntax", syntax)
    assert len(g._ss_pending_acks) == 1
    assert not g._ss_ack_failure_suppresses(
        "verify.horizon.executed", covering, identity
    )

    monkeypatch.setattr(g, "_ledger_line_direct", lambda _row: None)
    g._action_count = 4
    g._ss_scan_acks("I will repair src/parser.py now")

    assert g._ss_ack_failure_suppresses(
        "verify.horizon.executed", covering, identity
    )


def test_byte_change_and_distinct_diagnostic_reopen_eligibility(monkeypatch) -> None:
    _reset(monkeypatch)
    old = build_syntax_failure_identity(
        "src/parser.py", "a" * 64,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )
    changed_bytes = build_syntax_failure_identity(
        "src/parser.py", "b" * 64,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )
    changed_message = build_syntax_failure_identity(
        "src/parser.py", "a" * 64,
        "src/parser.py:7: SyntaxError: unexpected indent",
    )
    assert old is not None and changed_bytes is not None and changed_message is not None
    g._ss_acknowledged_failure_identities.add(old)

    assert g._ss_ack_failure_suppresses("edit.syntax", "old", old)
    assert not g._ss_ack_failure_suppresses("edit.syntax", "new-state", changed_bytes)
    assert not g._ss_ack_failure_suppresses("edit.syntax", "new-error", changed_message)


def test_incomplete_identity_never_suppresses(monkeypatch) -> None:
    _reset(monkeypatch)
    assert not g._ss_ack_failure_suppresses("edit.syntax", "diagnostic", None)


def test_incomplete_same_byte_candidate_prevents_stale_receipt_attribution(
    monkeypatch,
) -> None:
    _reset(monkeypatch)
    identity = build_syntax_failure_identity(
        "src/parser.py", "a" * 64,
        "src/parser.py:7: SyntaxError: invalid syntax",
    )
    assert identity is not None
    text = "src/parser.py:7: SyntaxError: invalid syntax"
    assert not g._ss_ack_failure_suppresses("edit.syntax", text, identity)
    assert not g._ss_ack_failure_suppresses("edit.syntax", text, None)

    g._action_count = 2
    g._ss_note_delivery_for_ack("edit.syntax", text)
    assert "failure_identity" not in g._ss_pending_acks[0]


def test_edit_syntax_producer_reopens_after_exact_byte_change(
    monkeypatch, tmp_path: Path
) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir()
    source.write_text("return (\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_build_edit_check_executor", lambda: None)
    diagnostic = 'File "src/parser.py", line 7\nSyntaxError: invalid syntax'
    import groundtruth.runtime.edit_check as edit_check
    monkeypatch.setattr(
        edit_check,
        "check_edit_syntax",
        lambda *_a, **_k: {
            "verdict": "syntax_error",
            "diagnostic": diagnostic,
        },
    )
    old = g._ss_build_syntax_failure_identity("src/parser.py", diagnostic)
    assert old is not None
    g._ss_acknowledged_failure_identities.add(old)

    assert g._edit_syntax_candidate("src/parser.py") is None

    source.write_text("return [\n", encoding="utf-8")
    reopened = g._edit_syntax_candidate("src/parser.py")
    assert reopened is not None
    assert reopened[1] == "edit.syntax"


def test_covering_red_producer_suppresses_only_acknowledged_exact_state(
    monkeypatch, tmp_path: Path
) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir()
    source.write_text("return (\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_parser.py"
    test_file.parent.mkdir()
    test_file.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_last_test_step", -1)
    monkeypatch.setattr(g, "_action_count", 4)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("src/parser.py")
    result = {
        "verdict": "fail",
        "ran": ["tests/test_parser.py"],
        "stdout_tail": "src/parser.py:7: SyntaxError: invalid syntax",
        "stderr_tail": "",
    }
    import groundtruth.runtime.covering_runner as covering_runner
    monkeypatch.setattr(covering_runner, "run_covering_tests", lambda *_a, **_k: result)
    monkeypatch.setattr(covering_runner, "is_red_attributable", lambda *_a, **_k: True)
    old = g._ss_covering_failure_identity(result)
    assert old is not None
    g._ss_acknowledged_failure_identities.add(old)

    assert g._executed_covering_emission(
        [{"file": "tests/test_parser.py"}],
        {"src/parser.py"},
        {"parse_item"},
    ) is None

    source.write_text("return [\n", encoding="utf-8")
    reopened = g._executed_covering_emission(
        [{"file": "tests/test_parser.py"}],
        {"src/parser.py"},
        {"parse_item"},
    )
    assert reopened is not None


def test_edit_delivery_ack_then_cross_class_covering_suppression(
    monkeypatch, tmp_path: Path
) -> None:
    """Actual producer -> existing watcher -> other producer, with no direct promotion."""
    _reset(monkeypatch)
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir()
    source.write_text("return (\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_parser.py"
    test_file.parent.mkdir()
    test_file.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_build_edit_check_executor", lambda: None)
    monkeypatch.setattr(g, "_last_test_step", -1)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("src/parser.py")
    diagnostic = 'File "src/parser.py", line 7\nSyntaxError: invalid syntax'
    import groundtruth.runtime.edit_check as edit_check
    monkeypatch.setattr(
        edit_check,
        "check_edit_syntax",
        lambda *_a, **_k: {"verdict": "syntax_error", "diagnostic": diagnostic},
    )
    result = {
        "verdict": "fail",
        "ran": ["tests/test_parser.py"],
        "stdout_tail": "src/parser.py:7: SyntaxError: invalid syntax",
        "stderr_tail": "",
    }
    import groundtruth.runtime.covering_runner as covering_runner
    monkeypatch.setattr(covering_runner, "run_covering_tests", lambda *_a, **_k: result)
    monkeypatch.setattr(covering_runner, "is_red_attributable", lambda *_a, **_k: True)

    g._action_count = 3
    edit_candidate = g._edit_syntax_candidate("src/parser.py")
    assert edit_candidate is not None
    g._ss_note_delivery_for_ack(edit_candidate[1], edit_candidate[2])

    # The equivalent other producer remains eligible before acknowledgment.
    before_ack = g._executed_covering_emission(
        [{"file": "tests/test_parser.py"}], {"src/parser.py"}, {"parse_item"}
    )
    assert before_ack is not None

    monkeypatch.setattr(g, "_ledger_line_direct", lambda _row: None)
    g._action_count = 4
    g._ss_scan_acks("I will repair src/parser.py now")

    after_ack = g._executed_covering_emission(
        [{"file": "tests/test_parser.py"}], {"src/parser.py"}, {"parse_item"}
    )
    assert after_ack is None


def test_covering_payload_with_additional_value_fact_is_not_suppressed(
    monkeypatch, tmp_path: Path
) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir()
    source.write_text("return (\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_parser.py"
    test_file.parent.mkdir()
    test_file.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_last_test_step", -1)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("src/parser.py")
    result = {
        "verdict": "fail",
        "ran": ["tests/test_parser.py"],
        "stdout_tail": (
            "src/parser.py:7: SyntaxError: invalid syntax\n"
            "Expected: preserved value"
        ),
        "stderr_tail": "",
    }
    import groundtruth.runtime.covering_runner as covering_runner
    monkeypatch.setattr(covering_runner, "run_covering_tests", lambda *_a, **_k: result)
    monkeypatch.setattr(covering_runner, "is_red_attributable", lambda *_a, **_k: True)
    acknowledged = g._ss_covering_failure_identity(result)
    assert acknowledged is not None
    g._ss_acknowledged_failure_identities.add(acknowledged)

    delivered = g._executed_covering_emission(
        [{"file": "tests/test_parser.py"}], {"src/parser.py"}, {"parse_item"}
    )

    assert delivered is not None
    assert "Expected: preserved value" in delivered


def test_unrecognized_covering_framing_cannot_discard_semantic_content(
    monkeypatch, tmp_path: Path
) -> None:
    _reset(monkeypatch)
    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir()
    source.write_text("return (\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("src/parser.py")
    result = {
        "stdout_tail": "src/parser.py:7: SyntaxError: invalid syntax",
        "stderr_tail": "",
    }

    identity = g._ss_covering_failure_identity(
        result,
        "Novel semantic fact\nsrc/parser.py:7: SyntaxError: invalid syntax",
    )

    assert identity is None
