"""Artifact-first TTD: steer dedup suppresses repeated identical steers.

Frozen artifact: tests/fixtures/trajectory/lsp_13453/gt_hook_telemetry.jsonl
Evidence: 5 steers all targeting html.py, 4 are repeats after agent
already demonstrated alignment by editing html.py.

Expected: a dedup function should identify that steers 3-5 are suppressible
noise (same file, agent already editing, no new evidence).

Steers to DIFFERENT files must NOT be suppressed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trajectory"

try:
    from scripts.swebench.steer_dedup import should_suppress_steer
except ImportError:
    should_suppress_steer = None

requires_dedup = pytest.mark.skipif(
    should_suppress_steer is None,
    reason="steer_dedup not implemented yet — test is RED"
)


def _load_events(case: str) -> list[dict]:
    p = FIXTURES / case / "gt_hook_telemetry.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _extract_steers_and_edits(events: list[dict]):
    """Extract (steer_file, cycle) pairs and edit history for dedup checks."""
    steers = []
    edited_files: set[str] = set()
    steer_history: list[str] = []

    for ev in events:
        e = ev.get("event", "")
        if e == "material_edit":
            for f in ev.get("files", []):
                edited_files.add(f)
        elif e == "steer_delivered":
            sf = ev.get("file", "")
            steers.append({
                "file": sf,
                "cycle": ev.get("cycle", 0),
                "channel": ev.get("channel", ""),
                "edited_files_at_time": set(edited_files),
                "prior_steers": list(steer_history),
            })
            steer_history.append(sf)

    return steers


@requires_dedup
def test_lsp_13453_steers_3_to_5_are_suppressible():
    """From frozen lsp_13453 trace: 5 steers to html.py.

    Steer 1 (cycle 14): first steer, not suppressible.
    Steer 2 (cycle 19): repeat, but only 1 prior → allowed (count <= 2).
    Steers 3-5 (cycles 23, 25, 30): same file, agent already editing it,
    repeated count > 2 → suppressible.

    Expected: should_suppress_steer returns True for steers 3-5.
    """
    events = _load_events("lsp_13453")
    steers = _extract_steers_and_edits(events)

    assert len(steers) == 5, f"Expected 5 steers, got {len(steers)}"

    # Steers 1-2: not suppressed
    assert should_suppress_steer(steers[0]) is False, "First steer is never suppressed"
    assert should_suppress_steer(steers[1]) is False, "Second steer to same file is allowed"

    # Steers 3-5: suppressed (repeated > 2, agent already editing)
    for i in [2, 3, 4]:
        assert should_suppress_steer(steers[i]) is True, (
            f"Steer {i+1} to same file after agent already editing it "
            f"and repeated count > 2 must be suppressed. "
            f"file={steers[i]['file']}, prior_steers={steers[i]['prior_steers']}"
        )


@requires_dedup
def test_steers_to_different_files_not_suppressed():
    """Negative control: steers to different files must NOT be suppressed,
    even if the count is high."""
    steers = [
        {"file": "file_A.py", "cycle": 5, "channel": "micro",
         "edited_files_at_time": set(), "prior_steers": []},
        {"file": "file_B.py", "cycle": 10, "channel": "micro",
         "edited_files_at_time": {"file_A.py"}, "prior_steers": ["file_A.py"]},
        {"file": "file_C.py", "cycle": 15, "channel": "micro",
         "edited_files_at_time": {"file_A.py", "file_B.py"},
         "prior_steers": ["file_A.py", "file_B.py"]},
    ]
    for i, s in enumerate(steers):
        assert should_suppress_steer(s) is False, (
            f"Steer {i+1} to a NEW file ({s['file']}) must not be suppressed"
        )


@requires_dedup
def test_repeated_steer_without_agent_editing_not_suppressed():
    """If the agent has NOT edited the steered file yet, repeating the
    steer is potentially useful (agent may not have seen it). Do not suppress."""
    steers = [
        {"file": "target.py", "cycle": 5, "channel": "micro",
         "edited_files_at_time": set(), "prior_steers": []},
        {"file": "target.py", "cycle": 10, "channel": "material_edit",
         "edited_files_at_time": set(), "prior_steers": ["target.py"]},
        {"file": "target.py", "cycle": 15, "channel": "material_edit",
         "edited_files_at_time": set(),
         "prior_steers": ["target.py", "target.py"]},
    ]
    # Agent never edited target.py → repeats may be needed
    for i, s in enumerate(steers):
        assert should_suppress_steer(s) is False, (
            f"Steer {i+1} to target.py but agent hasn't edited it yet. "
            f"Do not suppress — agent may need the reminder."
        )
