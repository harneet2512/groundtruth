"""Steer dedup: suppress repeated identical steers that add no information.

A steer is suppressible when:
1. Same focus file as a prior steer (repeated)
2. Agent has already edited that file (behavioral alignment demonstrated)
3. Repeated count > 2 (allow 1 re-delivery as a reminder, suppress 3+)

A steer is NOT suppressible when:
- It targets a file not previously steered (new information)
- The agent has NOT edited the steered file (may still need the guidance)
- It's the first or second delivery to that file (allow initial + one retry)
"""
from __future__ import annotations


def should_suppress_steer(steer: dict) -> bool:
    """Decide whether a steer delivery should be suppressed as noise.

    Args:
        steer: dict with keys:
            file: the focus file this steer targets
            prior_steers: list of files from previously delivered steers
            edited_files_at_time: set of files the agent has edited so far

    Returns True if the steer should be suppressed (noise), False if it
    should be delivered (potentially useful).
    """
    target = steer.get("file", "")
    if not target:
        return False

    prior = steer.get("prior_steers", [])
    edited = steer.get("edited_files_at_time", set())

    prior_count = sum(1 for p in prior if p == target)

    if prior_count < 2:
        return False

    if target not in edited:
        return False

    return True
