"""Correct-or-quiet: a scaffold-only minimal brief must ship NOTHING, not a
33-char hollow ``<gt-task-brief>\\n\\n</gt-task-brief>`` (run6 audit D-3).

Live kill: smolagents/babel/privacyidea/mpl-29721 each sealed a 33-byte empty
wrapper as a `brief.task` delivery with `block_lineage: []` — a delivery that
carries zero model-facing fact, counted as delivered, dosing the task_start slot.

RED: the old reducer returned the bare scaffold (len 33). GREEN: it returns "".
Regression: a brief WITH an obligations block is byte-unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "src"),):
    if p not in sys.path:
        sys.path.insert(0, p)

from groundtruth.pretask import v1r_brief as vb  # noqa: E402


_HOLLOW = "<gt-task-brief>\n\n</gt-task-brief>"


def test_scaffold_only_brief_reduces_to_empty():
    # exactly the shape the run6 tasks shipped: tags + blank body, nothing else
    assert len(_HOLLOW) == 33  # the literal "33" the audit named
    out = vb._reduce_brief_to_minimal(_HOLLOW)
    assert out == "", f"scaffold-only brief must be empty, got {out!r} (len {len(out)})"


def test_scaffold_with_only_dropped_blocks_reduces_to_empty():
    # a brief whose only substantive block is a droppable graph-map/HIGH-localization
    brief = (
        "<gt-task-brief>\n"
        "<gt-graph-map>\n  a.py -> b.py\n</gt-graph-map>\n"
        "</gt-task-brief>"
    )
    out = vb._reduce_brief_to_minimal(brief)
    assert out == "", f"all-droppable brief must be empty, got {out!r}"


def test_obligations_brief_is_preserved():
    brief = (
        "<gt-task-brief>\n"
        "<gt-obligations>\n  - [ ] handle the empty case\n</gt-obligations>\n"
        "</gt-task-brief>"
    )
    out = vb._reduce_brief_to_minimal(brief)
    assert out != ""
    assert "handle the empty case" in out
    assert "gt-obligations" in out


def test_real_no_anchor_orientation_note_is_PRESERVED():
    # The exact honest brief the audits praised (llama-factory, checkov): a
    # correct-or-quiet "could not anchor -> use grep" orientation note. This MUST
    # survive — the fix suppresses ONLY hollow scaffold, never this honest note.
    brief = (
        "<gt-task-brief>\n"
        "Note: GT could not anchor a candidate to the issue via a verified graph "
        "edge — use grep on issue keywords to confirm the edit target.\n"
        "</gt-task-brief>"
    )
    labels = [b["label"] for b in vb._segment_brief_blocks(brief)]
    assert "orientation-note" in labels, f"real note mislabeled: {labels}"
    out = vb._reduce_brief_to_minimal(brief)
    assert out != "", "the honest no-anchor orientation note must survive"
    assert "grep" in out
