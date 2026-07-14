"""SS-5 — the GT_BRIEF_NATIVE step-0 obligations checklist under GT_SS_ACK_FORM.

The requirement-extractor discipline (memo FORM RULE 1/7): the native step-0 obligations render as
a SHORT PLAIN checklist of IMPERATIVE requirement items only — repro fragments and pleas are
dropped, and the ``[<kind>]`` classifier prefix is stripped so each item reads as a native
requirement line. Layered ON TOP of GT_BRIEF_NATIVE so:
  * both flags OFF                     -> the ``<gt-obligations>`` tag block (byte-identical).
  * GT_BRIEF_NATIVE only (ACK off)     -> the ITEM-5 plain checklist WITH kind tags (byte-identical).
  * GT_BRIEF_NATIVE + GT_SS_ACK_FORM   -> imperative-only, kind-stripped checklist.
RED-first + biting mutations (a checklist that admits a repro/plea line must fail).
"""
from __future__ import annotations

import pytest

from groundtruth.pretask import v1r_brief as v

_ROWS = [
    "  - [behavior] the parser must coalesce adjacent ranges",
    "  - [repro] 1. run the failing command and observe the crash",
    "  - [signature] range() accepts an optional step argument",
]


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    monkeypatch.delenv("GT_BRIEF_NATIVE", raising=False)
    monkeypatch.delenv("GT_SS_ACK_FORM", raising=False)
    yield


# ── flag parse family ────────────────────────────────────────────────────────
@pytest.mark.parametrize("val", ["", "0", "off", "false", "no"])
def test_ack_flag_off(monkeypatch, val):
    if val == "":
        monkeypatch.delenv("GT_SS_ACK_FORM", raising=False)
    else:
        monkeypatch.setenv("GT_SS_ACK_FORM", val)
    assert v._ss_ack_form_on() is False


def test_ack_flag_on(monkeypatch):
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    assert v._ss_ack_form_on() is True


# ── byte-identity of the two pre-existing arms ───────────────────────────────
def test_tag_arm_byte_identical(monkeypatch):
    # both flags off -> the <gt-obligations> tag block, unchanged.
    assert v._obligations_section(_ROWS) == [
        "", "<gt-obligations>",
        "  - [behavior] the parser must coalesce adjacent ranges",
        "  - [repro] 1. run the failing command and observe the crash",
        "  - [signature] range() accepts an optional step argument",
        "</gt-obligations>",
    ]


def test_native_only_arm_byte_identical(monkeypatch):
    # GT_BRIEF_NATIVE on, ACK off -> the ITEM-5 plain checklist WITH kind tags, all rows.
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    assert v._obligations_section(_ROWS) == [
        "", "Requirements to satisfy (from the issue):",
        "- [ ] [behavior] the parser must coalesce adjacent ranges",
        "- [ ] [repro] 1. run the failing command and observe the crash",
        "- [ ] [signature] range() accepts an optional step argument",
    ]


# ── the SS-5 ACK arm ─────────────────────────────────────────────────────────
def test_ack_arm_drops_repro_and_strips_kind(monkeypatch):
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    out = v._obligations_section(_ROWS)
    assert out == [
        "", "Requirements to satisfy (from the issue):",
        "- [ ] the parser must coalesce adjacent ranges",
        "- [ ] range() accepts an optional step argument",
    ]
    # MUTATION EVIDENCE: the dropped repro fragment must NOT appear anywhere in the output.
    assert not any("run the failing command" in line for line in out)
    # and the kind classifier tag is stripped (no "[behavior]"/"[signature]" survives).
    assert not any("[behavior]" in line or "[signature]" in line for line in out)


def test_ack_arm_all_repro_is_quiet(monkeypatch):
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    assert v._obligations_section(["  - [repro] 1. do x", "  - [repro] 2. do y"]) == []


def test_ack_requires_brief_native(monkeypatch):
    # GT_SS_ACK_FORM alone (GT_BRIEF_NATIVE off) does NOT change the tag arm — it layers on native.
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    out = v._obligations_section(_ROWS)
    assert out[1] == "<gt-obligations>"  # still the tag block


# ── the imperative predicate (truth table) ───────────────────────────────────
@pytest.mark.parametrize("kind,text,expected", [
    ("behavior", "the parser must coalesce adjacent ranges", True),
    ("signature", "range() accepts an optional step argument", True),
    ("error", "raise ValueError on an empty input", True),
    ("compat", "the old config keys continue to work", True),
    ("repro", "1. run the failing command", False),          # repro fragment
    ("repro", "expected 5 but got 3", False),
    ("behavior", "Please add a flag for this", False),        # plea
    ("behavior", "could you make this configurable", False),
    ("behavior", "I think a TypeError would be helpful to users", True),
    ("behavior", "It would be helpful to return an empty list", True),
    ("behavior", "I would like the parser to preserve ordering", True),
    ("behavior", "why does it fail on empty input?", False),  # question
    ("behavior", "", False),                                  # empty
])
def test_is_imperative_truth_table(kind, text, expected):
    assert v._obligation_is_imperative(kind, text) is expected


def test_parse_obligation_row_roundtrip():
    assert v._parse_obligation_row("  - [behavior] do the thing") == ("behavior", "do the thing")
    assert v._parse_obligation_row("  - a bare requirement") == ("", "a bare requirement")


# ── MUTATION EVIDENCE: the predicate BITES on the exact classes it must drop ──
def test_predicate_bites_on_repro_and_plea():
    # if the repro drop were removed, THIS reddens.
    assert v._obligation_is_imperative("repro", "1. reproduce with foo") is False
    # if the plea/question drop were removed, THESE redden.
    assert v._obligation_is_imperative("behavior", "please fix the crash") is False
    assert v._obligation_is_imperative("behavior", "is there a way to disable this?") is False
    # a genuine requirement is KEPT (the filter is not a nuke).
    assert v._obligation_is_imperative("behavior", "return None when the list is empty") is True


def test_ack_output_carries_no_gt_tag(monkeypatch):
    # the native checklist rides the model's requirements-list channel — no <gt-*> frame.
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    out = v._obligations_section(_ROWS)
    assert not any("<gt-" in line for line in out)
