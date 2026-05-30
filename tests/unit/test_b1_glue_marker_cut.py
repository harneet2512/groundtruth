"""TTD — TASK C1a (B1): glue / marker-cut at the delivery boundary.

Artifact-first reference (real trajectories):
  - haystack: an agent observation was corrupted to "...text wit# SPDX..." — GT
    content fused directly onto the file banner with no separator.
  - beets: a GT block was cut mid-marker so "[CATCHES]" became "[CATCHE" and was
    glued directly onto the following text "Here's the result of running cat -n".

Root cause in oh_gt_full_wrapper.py:
  The L3 post-edit path raw-sliced the evidence body to a fixed char cap
  (``hook_body = hook_body[:1997] + "..."``). A fixed byte slice can land
  MID-MARKER ("[CATCHE") or MID-WORD, and the "..." was glued directly onto the
  cut with no line boundary.

This test exercises the wrapper's own truncation helper directly (no OH harness):
  (red)   the fixed-byte slice CAN cut a marker to "[CATCHE" and glue "..." onto it
  (green) the safe-boundary truncation never emits a partial marker, the omission
          note sits on its own line, and append never fuses onto the prior obs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "swebench"))
sys.modules.setdefault(
    "litellm",
    SimpleNamespace(
        model_cost={}, success_callback=[],
        completion=lambda *a, **k: None, acompletion=None,
        completion_cost=lambda *a, **k: 0.0,
    ),
)

from scripts.swebench import oh_gt_full_wrapper as ohgt  # noqa: E402


def _body_that_cuts_marker_at_1997() -> str:
    """A body whose char index 1997 lands inside the "[CATCHES]" marker, so a naive
    body[:1997] cut yields the corrupted "[CATCHE" fragment."""
    marker = "[CATCHES] ValueError"
    tail = "\nHere's the result of running cat -n"
    # Place the marker so its 8th char (index after "[CATCHE") sits at position 1997.
    # "[CATCHE" is 7 chars; we want the cut between "[CATCHE" and "S]".
    prefix_len = 1997 - len("[CATCHE")
    prefix = "x" * prefix_len
    return prefix + marker + tail


def test_raw_slice_reproduces_marker_cut_and_glue():
    """RED-anchor (pure-Python): the OLD fixed-byte slice produces "[CATCHE...".

    This proves the *mechanism* of the defect independent of the wrapper, so the
    test is not tautological with the implementation.
    """
    body = _body_that_cuts_marker_at_1997()
    assert len(body) > 2000, "fixture must exceed the cap to force truncation"
    old = body[:1997] + "..."
    # The marker name is cut and "..." is glued directly onto the fragment.
    assert old.endswith("[CATCHE..."), f"raw slice must cut the marker, tail={old[-15:]!r}"


def test_safe_truncation_helper_exists():
    assert hasattr(ohgt, "_safe_truncate_evidence"), (
        "fix must add _safe_truncate_evidence(text, max_chars) safe-boundary truncator"
    )


def test_safe_truncation_never_leaves_partial_marker():
    """GREEN: safe truncation never leaves a dangling "[CATCHE" marker fragment."""
    body = _body_that_cuts_marker_at_1997()
    out = ohgt._safe_truncate_evidence(body, 2000)
    # Every "[CATCHE" occurrence must be the full "[CATCHES]" marker, never a stub.
    idx = 0
    while True:
        idx = out.find("[CATCHE", idx)
        if idx == -1:
            break
        assert out[idx:idx + len("[CATCHES]")] == "[CATCHES]", (
            f"partial marker leaked at {idx}: {out[idx:idx + 12]!r}"
        )
        idx += 1


def test_safe_truncation_omission_note_on_own_line():
    """GREEN: when content is omitted the note is on its OWN line — never glued onto
    the preceding word."""
    body = "alpha beta gamma " * 400  # ~6800 chars, well over the cap
    out = ohgt._safe_truncate_evidence(body, 2000)
    assert len(out) <= 2000 + 60, f"must respect the cap (+note), got {len(out)}"
    note_lines = [ln for ln in out.split("\n") if "omitted" in ln.lower()]
    assert note_lines, f"expected an omission note line, tail={out[-80:]!r}"
    note = note_lines[-1]
    note_pos = out.rfind(note)
    # The char immediately before the note must be a newline (own-line guarantee).
    assert note_pos == 0 or out[note_pos - 1] == "\n", (
        f"omission note must start a fresh line, prev char={out[note_pos - 1]!r}"
    )
    # And the kept content ends on a whole word, never mid-word.
    kept = out[:note_pos].rstrip()
    assert kept.endswith(("gamma", "beta", "alpha")), f"cut mid-word: {kept[-20:]!r}"


def test_safe_truncation_no_op_under_cap():
    """A body within the cap is returned unchanged (no spurious note)."""
    body = "[CATCHES] ValueError\n[GT] short evidence"
    out = ohgt._safe_truncate_evidence(body, 2000)
    assert out == body, "content within the cap must be returned verbatim"


def test_append_observation_never_glues_onto_prior_obs():
    """End-to-end at the boundary: appended GT content always carries a newline
    delimiter from the prior observation (the 'text wit# SPDX' glue cannot recur)."""
    obs = SimpleNamespace(content="some file content ending mid-word text wit")
    gt = "[CATCHES] ValueError\n[GT] evidence body"
    out = ohgt.append_observation(obs, gt)
    assert "text wit[CATCHES]" not in out.content, "GT fused onto prior obs (glue defect)"
    assert "text wit\n" in out.content, (
        f"missing clean delimiter, content tail: {out.content[-60:]!r}"
    )
