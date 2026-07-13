"""SS-5 — the m1 preamble ACK-FORM arm (GT_SS_ACK_FORM), gt_agent.py.

WHY (evidence base): the 296-delivery causal audit measured ~3% acknowledgment, and HALF of
those were dismissals that NAMED GroundTruth as a tool ("just the ground truth tool automatically
checking"). The legacy ``_GT_PREAMBLE`` TAUGHT that discount: it framed the inline evidence as a
named tool's output ("GroundTruth automatically appends ... inside <gt-evidence> tags ... Read
those tags"), AND documented a ``<gt-*>`` tag grammar that no longer exists (payloads are tagless
native since W10). SS-5 replaces it, flag-gated, with native environment-diagnostic guidance —
no tool-framing, no retired-tag documentation, no "look for markers", and NO deception (it
describes environment diagnostics, which is exactly what the inline evidence is).

INVARIANTS pinned here (RED-first + biting mutations):
  * flag OFF  -> ``_automatic_preamble()`` is BYTE-IDENTICAL to the legacy text (a hardcoded pin,
    so any drift of ``_GT_PREAMBLE`` or the selection reddens).
  * flag ON   -> the ACK-FORM text: 0 ``<gt-*>`` tags, 0 tool-framing tokens, 0 "read the tags"
    instruction; the engineering substance (callers / contract / verify-with-tests) survives.
  * research C1: NO authority/confidence markers are added (they do not raise uptake).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# gt_agent lives under artifact_deepswe/ (the pier adapter); make it importable.
_ART = Path(__file__).resolve().parents[2] / "artifact_deepswe"
if str(_ART) not in sys.path:
    sys.path.insert(0, str(_ART))

import gt_agent  # noqa: E402

# The EXACT legacy preamble bytes (captured from the pre-SS-5 tree). This is the byte-identity
# oracle: if a future edit changes _GT_PREAMBLE or the flag-off selection, THIS reddens.
_LEGACY_PREAMBLE = (
    "\n## GroundTruth codebase intelligence (automatic)\n\n"
    "As you read and edit files, GroundTruth automatically appends evidence to the\n"
    "command output inside <gt-evidence> tags: who calls a function and how, how many\n"
    "tests cover it, behavioral contracts (signature/return), and sibling\n"
    "patterns you must match. Read those tags -- they are cross-file facts you\n"
    "cannot get from the file alone. They appear on their own; you do not call\n"
    "anything. When GT shows callers, do not break them; when it shows a contract,\n"
    "preserve it; and run your own targeted tests to verify each change before you finish.\n"
)

# The form-rule predicates (the SS-0 ack watcher / leak invariants use the same shapes).
_GT_TAG_RE = re.compile(r"<gt-", re.IGNORECASE)
_SELF_ID_RE = re.compile(r"groundtruth|ground[ -]truth|\bgt\b", re.IGNORECASE)
_TOOL_FRAMING_RE = re.compile(
    r"automatically appends|read those tags|\btool\b|look for|inside <gt", re.IGNORECASE)
_CONF_MARKER_RE = re.compile(r"\[VERIFIED\]|\[WARNING\]|\[INFO\]|high[- ]confidence|certified",
                             re.IGNORECASE)


@pytest.fixture(autouse=True)
def _clean_flag(monkeypatch):
    monkeypatch.delenv("GT_SS_ACK_FORM", raising=False)
    yield


# ── flag parse family ────────────────────────────────────────────────────────
@pytest.mark.parametrize("val", ["", "0", "off", "false", "no", "OFF", " 0 "])
def test_flag_off_values(monkeypatch, val):
    if val == "":
        monkeypatch.delenv("GT_SS_ACK_FORM", raising=False)
    else:
        monkeypatch.setenv("GT_SS_ACK_FORM", val)
    assert gt_agent._ss_ack_form_on() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_flag_on_values(monkeypatch, val):
    monkeypatch.setenv("GT_SS_ACK_FORM", val)
    assert gt_agent._ss_ack_form_on() is True


# ── byte-identity when OFF (the load-bearing safety pin) ─────────────────────
def test_flag_off_is_byte_identical_to_legacy(monkeypatch):
    monkeypatch.delenv("GT_SS_ACK_FORM", raising=False)
    assert gt_agent._automatic_preamble() == _LEGACY_PREAMBLE
    # the constant itself is unmoved
    assert gt_agent._GT_PREAMBLE == _LEGACY_PREAMBLE


def test_flag_on_selects_ack_form(monkeypatch):
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    assert gt_agent._automatic_preamble() == gt_agent._GT_PREAMBLE_ACK_FORM
    # and it is genuinely DIFFERENT from the legacy text (not an accidental alias)
    assert gt_agent._GT_PREAMBLE_ACK_FORM != _LEGACY_PREAMBLE


# ── ACK-FORM content rules (the discount-training removal) ────────────────────
def test_ack_form_has_no_retired_gt_tag_documentation():
    assert not _GT_TAG_RE.search(gt_agent._GT_PREAMBLE_ACK_FORM)
    # MUTATION EVIDENCE: the detector BITES — the legacy text DOES document <gt-*> tags.
    assert _GT_TAG_RE.search(_LEGACY_PREAMBLE)


def test_ack_form_does_not_self_identify_as_a_tool():
    assert not _SELF_ID_RE.search(gt_agent._GT_PREAMBLE_ACK_FORM)
    assert not _TOOL_FRAMING_RE.search(gt_agent._GT_PREAMBLE_ACK_FORM)
    # MUTATION EVIDENCE: both detectors BITE on the legacy tool-framing text.
    assert _SELF_ID_RE.search(_LEGACY_PREAMBLE)
    assert _TOOL_FRAMING_RE.search(_LEGACY_PREAMBLE)


def test_ack_form_adds_no_authority_or_confidence_markers():
    # research C1: assertiveness/confidence markers do not raise uptake — SS-5 must not add them.
    assert not _CONF_MARKER_RE.search(gt_agent._GT_PREAMBLE_ACK_FORM)


def test_ack_form_is_honest_environment_diagnostics_framing():
    # not deception: it describes inline environment diagnostics (which is what they are).
    low = gt_agent._GT_PREAMBLE_ACK_FORM.lower()
    assert "command output" in low
    assert "diagnostic" in low


def test_ack_form_preserves_the_engineering_substance():
    # the behavioral guidance the legacy text carried must survive (callers / contract / verify).
    low = gt_agent._GT_PREAMBLE_ACK_FORM.lower()
    assert "caller" in low
    assert "contract" in low
    assert "verify" in low and "test" in low


def test_manual_preamble_untouched():
    # the MANUAL (pull-tool) preamble is a genuine tool instruction and is NOT reframed by SS-5
    # (reframing it as env diagnostics would be deception — there is no automatic injection there).
    assert "gt_hook.py" in gt_agent._GT_MANUAL_PREAMBLE
    assert "Codebase Intelligence Tool" in gt_agent._GT_MANUAL_PREAMBLE
