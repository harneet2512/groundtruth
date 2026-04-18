"""Unit tests for the Phase-A ack classifier fix in swe_agent_state_gt.py.

Covers:
  - detect_material_edits_peek() is non-consuming (idempotent until the
    canonical detect_material_edits() runs and persists the new hashes).
  - _check_ack fires `ack_followed reason=targeted_edit_inferred` when the
    peeked edit_delta hits the focus file (action_head empty, e.g.
    str_replace_editor / bash where only gt_* wrappers write GT_LAST_ACTION).
  - _check_ack fires `ack_ignored reason=non_targeted_edit_inferred` when
    edit_delta is non-empty but disjoint from focus.
  - Mid-window silence is preserved when there's no action and no edit.
  - Window expiry still emits `ack_not_observed` when nothing happened.

Rerun8 returned ack_followed=0 / ack_ignored=0 / ack_not_observed=11 across
229 str_replace_editor invocations because `_current_action` was always ""
and `changed_files` was hard-wired to []. These tests lock in the fix.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def state_gt(tmp_path, monkeypatch):
    """Import the hook module with all filesystem state redirected to tmp_path."""
    import benchmarks.swebench.swe_agent_state_gt as m

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    monkeypatch.setattr(m, "GT_TELEMETRY", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(m, "GT_ACK_STATE", tmp_path / "gt_ack_state.json")
    monkeypatch.setattr(m, "GT_HASHES", tmp_path / "gt_file_hashes.json")
    monkeypatch.setattr(m, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(m, "REPO_ROOT", str(repo_root))
    monkeypatch.setattr(m, "_TELEM_HOST_DIR", "")
    return m


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _arm(state_gt, cycle: int, focus_file: str, focus_symbol: str = "") -> None:
    """Write an armed-ack snapshot straight to disk."""
    state_gt.GT_ACK_STATE.write_text(json.dumps({
        "cycle": cycle,
        "channel": "micro",
        "tier": "likely",
        "file": focus_file,
        "file_key": list(state_gt._file_suffix_key(focus_file)),
        "symbol": focus_symbol,
        "pre_emit_action": "",
        "pre_emit_changed": [],
        "pre_emit_file_refs": [],
        "pre_emit_symbol_refs": [],
        "expires_at_cycle": cycle + state_gt.NEXT_WINDOW_SIZE,
    }))


class TestInferredAckBranches:
    """Fixtures 1-4 from the Phase A plan."""

    def test_targeted_edit_inferred_fires_ack_followed(self, state_gt):
        """Fixture 1: peek reports focus file edited → ack_followed."""
        _arm(state_gt, cycle=5, focus_file="astropy/io/fits/hdu/table.py")
        state_gt._check_ack(
            cycle=6, action="",
            changed_files=["astropy/io/fits/hdu/table.py"],
        )
        events = _read_events(state_gt.GT_TELEMETRY)
        assert any(
            e["event"] == "ack_followed"
            and e.get("reason") == "targeted_edit_inferred"
            for e in events
        ), events
        assert not state_gt.GT_ACK_STATE.exists(), "ack state must be cleared"

    def test_non_targeted_edit_inferred_fires_ack_ignored(self, state_gt):
        """Fixture 2: peek reports a different file edited → ack_ignored."""
        _arm(state_gt, cycle=5, focus_file="astropy/io/fits/hdu/table.py")
        state_gt._check_ack(
            cycle=6, action="",
            changed_files=["astropy/io/registry.py"],
        )
        events = _read_events(state_gt.GT_TELEMETRY)
        assert any(
            e["event"] == "ack_ignored"
            and e.get("reason") == "non_targeted_edit_inferred"
            for e in events
        ), events
        assert not state_gt.GT_ACK_STATE.exists()

    def test_silent_midwindow_when_no_edit_no_action(self, state_gt):
        """Fixture 3: no edit + empty action inside window → no emit."""
        _arm(state_gt, cycle=5, focus_file="astropy/io/fits/hdu/table.py")
        # Window runs 5..11; cycle 7 is inside and must not emit anything.
        state_gt._check_ack(cycle=7, action="", changed_files=[])
        events = _read_events(state_gt.GT_TELEMETRY)
        assert not any(e["event"].startswith("ack_") for e in events), events
        assert state_gt.GT_ACK_STATE.exists(), "ack must remain armed inside window"

    def test_expiry_fires_ack_not_observed(self, state_gt):
        """Fixture 4: cycle >= expires_at with no edit/action → ack_not_observed."""
        _arm(state_gt, cycle=5, focus_file="astropy/io/fits/hdu/table.py")
        # NEXT_WINDOW_SIZE=6, so expires_at_cycle = 11.
        state_gt._check_ack(cycle=11, action="", changed_files=[])
        events = _read_events(state_gt.GT_TELEMETRY)
        assert any(e["event"] == "ack_not_observed" for e in events), events
        assert not state_gt.GT_ACK_STATE.exists()

    def test_targeted_inferred_matches_via_basename_when_paths_differ(self, state_gt):
        """Regression: edit_delta uses full repo path, focus file may differ
        in prefix. _file_suffix_key basename match must catch it."""
        _arm(state_gt, cycle=5, focus_file="src/astropy/io/fits/hdu/table.py")
        state_gt._check_ack(
            cycle=6, action="",
            # Same basename, different leading path — typical of git-diff output.
            changed_files=["astropy/io/fits/hdu/table.py"],
        )
        events = _read_events(state_gt.GT_TELEMETRY)
        assert any(
            e["event"] == "ack_followed"
            and e.get("reason") == "targeted_edit_inferred"
            for e in events
        ), events


class TestPeekIdempotency:
    """The peek helper must not consume the hash transition."""

    def test_peek_returns_same_set_until_consumed(self, state_gt, monkeypatch):
        repo_root = Path(state_gt.REPO_ROOT)
        focus = repo_root / "astropy" / "io" / "fits" / "hdu" / "table.py"
        focus.parent.mkdir(parents=True)
        focus.write_text("def foo(): return 1\n")

        # Seed GT_HASHES with a stale baseline → peek must report this file.
        state_gt.GT_HASHES.write_text(json.dumps({
            "astropy/io/fits/hdu/table.py": "stale-baseline-hash-not-matching",
        }))

        fake_run = MagicMock(return_value=MagicMock(
            stdout="astropy/io/fits/hdu/table.py\n", stderr="", returncode=0))
        monkeypatch.setattr(subprocess, "run", fake_run)

        first = state_gt.detect_material_edits_peek()
        second = state_gt.detect_material_edits_peek()
        assert first == ["astropy/io/fits/hdu/table.py"], first
        assert second == first, (first, second)

        # Hashes file must be untouched by peek.
        persisted = json.loads(state_gt.GT_HASHES.read_text())
        assert persisted == {"astropy/io/fits/hdu/table.py": "stale-baseline-hash-not-matching"}

        # Canonical consuming call returns the same set and updates hashes.
        consumed = state_gt.detect_material_edits()
        assert consumed == ["astropy/io/fits/hdu/table.py"]

        # Subsequent peek returns empty now that the baseline matches.
        after = state_gt.detect_material_edits_peek()
        assert after == [], after

    def test_peek_returns_empty_when_no_diff(self, state_gt, monkeypatch):
        """No git-reported changes → peek returns empty list."""
        fake_run = MagicMock(return_value=MagicMock(
            stdout="", stderr="", returncode=0))
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert state_gt.detect_material_edits_peek() == []
