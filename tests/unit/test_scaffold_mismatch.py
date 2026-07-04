"""Artifact-first TTD: detect model_scaffold_mismatch from trajectory.

Frozen artifact: tests/fixtures/trajectory/nogt_12907.traj
Evidence: First response is 20,608 chars with 43 code blocks. Parser picks
one action (submit). Intermediate edits never execute. Empty patch.

Expected from EXPECTED_BEHAVIOR_SCAFFOLD.md:
  EB-SCAFFOLD-1: > 5 code blocks in first response + <= 6 steps → mismatch
  EB-SCAFFOLD-2: multi-block ending in submit → empty patch predicted
  EB-SCAFFOLD-4: normal multi-step trace is NOT mismatch (negative control)

These tests call classify_scaffold_compatibility() which does NOT exist yet.
They MUST fail (skip) before implementation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trajectory"

try:
    from scripts.swebench.trajectory_classifier import classify_scaffold_compatibility
except ImportError:
    classify_scaffold_compatibility = None

requires_classifier = pytest.mark.skipif(
    classify_scaffold_compatibility is None,
    reason="classify_scaffold_compatibility not implemented yet — RED"
)


def _load_trajectory(name: str) -> dict:
    p = FIXTURES / name
    return json.loads(p.read_text(errors="replace"))


def _count_code_blocks(text: str) -> int:
    return len(re.findall(r"```", text)) // 2


# ═══════════════════════════════════════════════════════════════════
# TEST 1 — EB-SCAFFOLD-1: multi-block first response → mismatch
# ═══════════════════════════════════════════════════════════════════
@requires_classifier
def test_nogt_12907_classified_as_scaffold_mismatch():
    """From frozen nogt_12907.traj: 43 code blocks in first response,
    4 total steps. Must be classified as model_scaffold_mismatch."""
    traj = _load_trajectory("nogt_12907.traj")
    result = classify_scaffold_compatibility(traj)
    assert result["classification"] == "model_scaffold_mismatch", (
        f"43 blocks in first response, 4 steps → must be mismatch. "
        f"Got {result['classification']}"
    )
    assert result["first_response_code_blocks"] > 5
    assert result["total_steps"] <= 6


# ═══════════════════════════════════════════════════════════════════
# TEST 2 — EB-SCAFFOLD-2: multi-block + submit → empty patch
# ═══════════════════════════════════════════════════════════════════
@requires_classifier
def test_nogt_12907_predicts_empty_patch():
    """First response ends with ```submit```. Parser picks that one action.
    Intermediate edits never execute → empty patch expected."""
    traj = _load_trajectory("nogt_12907.traj")
    result = classify_scaffold_compatibility(traj)
    assert result["ends_with_submit"] is True
    assert result["predicted_empty_patch"] is True


# ═══════════════════════════════════════════════════════════════════
# TEST 3 — EB-SCAFFOLD-4: normal multi-step trace is NOT mismatch
# ═══════════════════════════════════════════════════════════════════
@requires_classifier
def test_normal_multistep_trace_is_not_mismatch():
    """Negative control: a trajectory with many steps and single-block
    responses is a normal interaction, not scaffold mismatch."""
    # Use the lsp_13453 trajectory (33 cycles, 5 edits, normal interaction)
    traj = _load_trajectory("lsp_13453/trajectory.traj")
    result = classify_scaffold_compatibility(traj)
    assert result["classification"] != "model_scaffold_mismatch", (
        "Normal multi-step trace with single-block responses must not "
        "be classified as scaffold mismatch."
    )


# ═══════════════════════════════════════════════════════════════════
# TEST 4 — artifact validation: confirm frozen data matches claim
# ═══════════════════════════════════════════════════════════════════
def test_frozen_nogt_12907_has_expected_shape():
    """Verify the frozen artifact matches our documented claims:
    - 4 steps
    - first assistant response > 15000 chars
    - > 30 code blocks in first response
    - ends with submit
    """
    traj = _load_trajectory("nogt_12907.traj")
    history = traj.get("history", traj.get("trajectory", []))
    assert len(history) == 4, f"Expected 4 steps, got {len(history)}"

    # First assistant response
    assistant_steps = [s for s in history if s.get("role") == "assistant"]
    assert len(assistant_steps) >= 1
    first = str(assistant_steps[0].get("content", ""))
    assert len(first) > 15000, f"Expected > 15000 chars, got {len(first)}"

    blocks = _count_code_blocks(first)
    assert blocks > 30, f"Expected > 30 code blocks, got {blocks}"

    assert "submit" in first[-200:].lower(), (
        "First response must end with a submit action"
    )
