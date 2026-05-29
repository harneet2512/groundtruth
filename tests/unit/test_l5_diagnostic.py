"""Tests for Layer 2.5/2.6 — L5 scaffold advisory + L5b reminder DIAGNOSTIC form.

Research basis: SWE-PRM (NeurIPS 2025, arXiv 2509.02360) — mid-trajectory
intervention helps resolution ONLY when diagnostic, never prescriptive.
Action-prescriptive feedback ("edit file X" / "Next action: do Y") lowered
success and anchors the agent (arXiv 2412.06593, 2605.15184).

These tests lock the diagnostic-only contract:
- L5 scaffold advisory states the verifiable fact, NO file list, NO directive
- L5b reminder states the unexamined-signal observation, NO "Next action:"
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))
import oh_gt_full_wrapper as w  # noqa: E402


def _make_config():
    cfg = w.GTRuntimeConfig()
    cfg.brief_candidates = {"src/foo.py", "src/bar.py", "src/baz.py"}
    cfg._l5_metrics = {}
    return cfg


def test_scaffold_advisory_states_fact_no_file_list():
    """Advisory must state the verifiable diagnostic fact and NOT list files."""
    cfg = _make_config()
    out = w._render_scaffold_advisory("reproduce_bug.py", cfg)
    # The scaffold file name appears (verifiable fact about the trajectory)
    assert "reproduce_bug.py" in out
    # Diagnostic fact present
    assert "No tracked source file modified" in out
    # NO prescriptive file list — none of the candidate files appear
    assert "src/foo.py" not in out
    assert "src/bar.py" not in out
    assert "src/baz.py" not in out


def test_scaffold_advisory_no_directive_verbs():
    """No prescriptive 'Edit X first' / 'Start with' / 'Do not' directives."""
    cfg = _make_config()
    out = w._render_scaffold_advisory("scratch.py", cfg)
    lowered = out.lower()
    assert "edit source files first" not in lowered
    assert "start with" not in lowered
    assert "do not create" not in lowered


def test_scaffold_advisory_no_grep_directive():
    """The 'use gt_search / locate with grep' directive is also removed —
    that is still a steer, not a diagnostic."""
    cfg = _make_config()
    out = w._render_scaffold_advisory("scratch.py", cfg)
    assert "gt_search" not in out.lower()
    assert "grep" not in out.lower()


def test_scaffold_advisory_is_compact():
    """Diagnostic advisory should be short (high signal density)."""
    cfg = _make_config()
    out = w._render_scaffold_advisory("scratch.py", cfg)
    # advisory tag open + one fact line + close = 3 lines
    assert out.count("\n") <= 3


def test_scaffold_advisory_no_brief_candidates_still_fact():
    """Even with no brief candidates, the diagnostic fact still renders
    (it does not depend on having files to suggest)."""
    cfg = w.GTRuntimeConfig()
    cfg.brief_candidates = set()
    cfg._l5_metrics = {}
    out = w._render_scaffold_advisory("scratch.py", cfg)
    assert "No tracked source file modified" in out
    assert "scratch.py" in out
