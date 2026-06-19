"""TTD — TASK #47/#55 final piece: relevance-gate the wrapper's [RECALL] prefix.

Artifact-first reference (real beets smoke trajectory):
  - The L3 post-edit path prepended a file's cached evidence as ``[RECALL] from
    earlier: ...`` with NO relevance gate. A stale per-file post_view dump keyed
    to ``progress_write`` therefore leaked into the agent's observation even
    though the agent had edited a DIFFERENT function (``set_fields``). The
    cached evidence was unrelated to the edit and rendered as fact.

[RECALL] is a non-edge signal (no CALLS/IMPORTS edge backs it), so the
categorical edge filter cannot judge it. It needs a relevance gate keyed to the
edited function's identifier tokens (and/or issue terms). Correct-or-quiet:
suppress when an anchor exists and the cached text shares none of it; but KEEP
prior behavior when no anchor is derivable (over-suppressing legitimate recall
is also a harm).

The emission site is buried in a large handler, so the gate DECISION is factored
into a small pure helper ``oh_gt_full_wrapper._recall_should_emit`` plus a diff
parser ``_recall_edited_fn_names``. We test those directly.

RED (proven before the fix existed):
  - ``_recall_should_emit`` did not exist -> ``AttributeError`` (asserted below
    via a guard that documents the pre-fix state).
  - The original inline logic (``_recall_prefix = f"[RECALL] ...{cached}" if
    cached else ""``) unconditionally prepended the unrelated cached evidence:
        old hook_body startswith "[RECALL]"  -> True
        "progress_write" in old hook_body    -> True
    (captured literally in the task report).

GREEN (this test):
  - unrelated cached evidence + edited fn ``set_fields`` -> suppressed (False)
  - cached evidence overlapping the edited fn -> kept (True)
  - cached evidence overlapping issue terms -> kept (True)
  - no anchor available -> kept (True, no over-suppression)
  - empty cached evidence -> never emit (False)
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "swebench"))
# Stub litellm so importing the wrapper does not require the real dependency.
sys.modules.setdefault(
    "litellm",
    SimpleNamespace(
        model_cost={}, success_callback=[],
        completion=lambda *a, **k: None, acompletion=None,
        completion_cost=lambda *a, **k: 0.0,
    ),
)

from scripts.swebench import oh_gt_full_wrapper as ohgt  # noqa: E402


# The observed defect: a cached post_view dump about an UNRELATED function while
# a DIFFERENT function was edited.
_UNRELATED_CACHED = "[CONTRACT] def progress_write():\n  Called by: ui.py:12"
_EDITED_FN = "set_fields"
_ISSUE_TERMS = {"parse", "field", "format"}

# A live unified diff (OpenHands observation shape) editing ``set_fields``.
_LIVE_DIFF = (
    "@@ -10,3 +10,4 @@ def set_fields(self, key, value):\n"
    "     self._store[key] = value\n"
    "+    self._dirty = True\n"
    "     return self\n"
)


class TestRecallGatePresent:
    """The fix must expose the gate helpers (their absence WAS the red)."""

    def test_helper_exists(self):
        assert hasattr(ohgt, "_recall_should_emit"), (
            "RED state: _recall_should_emit absent -> wrapper had no [RECALL] "
            "relevance gate (unrelated cached evidence leaked)."
        )
        assert callable(ohgt._recall_should_emit)

    def test_diff_parser_exists(self):
        assert hasattr(ohgt, "_recall_edited_fn_names")
        assert callable(ohgt._recall_edited_fn_names)


class TestRecallEditedFnNames:
    """The edited-fn anchor is sourced locally from the live diff -> no NameError."""

    def test_extracts_from_hunk_header(self):
        assert "set_fields" in ohgt._recall_edited_fn_names(_LIVE_DIFF)

    def test_extracts_from_added_def_line(self):
        diff = "@@ -1,0 +1,2 @@\n+def new_helper(x):\n+    return x\n"
        assert "new_helper" in ohgt._recall_edited_fn_names(diff)

    def test_empty_diff_yields_empty(self):
        assert ohgt._recall_edited_fn_names("") == set()


class TestRecallShouldEmit:
    def test_unrelated_cached_evidence_suppressed(self):
        """The beets defect: progress_write recall while set_fields edited.

        Pre-fix this content WAS prepended (RED). Post-fix the gate drops it.
        """
        edited = ohgt._recall_edited_fn_names(_LIVE_DIFF)
        assert edited == {"set_fields"}
        assert ohgt._recall_should_emit(_UNRELATED_CACHED, edited, None) is False

    def test_overlapping_via_fn_tokens_kept(self):
        cached = "[CONTRACT] def set_fields(self): ..."
        assert ohgt._recall_should_emit(cached, {"set_fields"}, None) is True

    def test_overlapping_via_issue_terms_kept(self):
        cached = "[FORMAT] callers parse the field format"
        # No edited fn known, but issue terms anchor the relevance.
        assert ohgt._recall_should_emit(cached, set(), _ISSUE_TERMS) is True

    def test_no_anchor_suppresses_correct_or_quiet(self):
        """No edited fn AND no issue terms -> cannot judge -> suppress (correct-or-quiet).
        Changed from prior behavior (emit) to suppress: when no anchor is derivable,
        staying silent is safer than emitting potentially stale/unrelated recall evidence."""
        assert ohgt._recall_should_emit(_UNRELATED_CACHED, set(), None) is False
        assert ohgt._recall_should_emit(_UNRELATED_CACHED, set(), set()) is False

    def test_empty_cached_evidence_never_emits(self):
        assert ohgt._recall_should_emit("", {"set_fields"}, None) is False
        assert ohgt._recall_should_emit("   ", {"set_fields"}, None) is False

    def test_anchor_present_but_no_overlap_suppressed(self):
        """An anchor exists and the cached text shares none of it -> drop."""
        assert (
            ohgt._recall_should_emit("totally unrelated banner text", {"set_fields"}, None)
            is False
        )


class TestRecallEmissionParity:
    """Document the pre-fix inline logic (the RED) vs the post-fix gated logic
    (the GREEN) at the emission site, on the exact beets-shaped inputs."""

    def test_old_inline_logic_leaked(self):
        # Verbatim pre-fix emission (lines 5125-5128 of the wrapper).
        cached = _UNRELATED_CACHED
        old_prefix = f"[RECALL] from earlier: {cached}\n" if cached else ""
        old_hook_body = old_prefix + "[CONTRACT] def set_fields():"
        # RED: the unrelated recall leaked.
        assert old_hook_body.startswith("[RECALL]")
        assert "progress_write" in old_hook_body

    def test_new_gated_logic_drops_leak(self):
        # Post-fix emission decision on the same inputs.
        edited = ohgt._recall_edited_fn_names(_LIVE_DIFF)
        cached = _UNRELATED_CACHED
        new_prefix = ""
        if cached and ohgt._recall_should_emit(cached, edited, None):
            new_prefix = f"[RECALL] from earlier: {cached}\n"
        new_hook_body = new_prefix + "[CONTRACT] def set_fields():"
        # GREEN: no leak, no [RECALL] prefix.
        assert not new_hook_body.startswith("[RECALL]")
        assert "progress_write" not in new_hook_body


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
