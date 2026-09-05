"""Consensus curation narrowing — the band-graduated shrinking of the candidate set.

This is the mechanism behind "consensus = GT and the agent reaching shared
understanding so the CURATION AREA decreases" (not exploration expansion — GT's
core product contract). The curation area is the set of candidate files GT keeps
in play. As the agent acts, it monotonically SHRINKS across three action bands:

    early (actions 0-5)   : full prior candidate set         — agent orients
    mid   (actions 5-10)  : drop visited-and-not-edited files — relevance feedback
    late  (actions 10+)   : keep only edit-connected files    — converge on the fix

The shrink is a strict FILTER on the running set (each band only removes), so the
size is monotonically non-increasing by construction. Every step emits a
structured ``[GT_CURATION]`` line (a hidden/diagnostic prefix, never shown to the
agent) so the shrink is PROVABLE from output.jsonl in the agent loop:

    [GT_CURATION] action=7 band=mid size=6 initial=10 dropped=4 reason=mid_drop_visited

DEFINITION OF DONE for this layer = those lines show the curation area converging
on a real trajectory; this module + its unit test prove the narrowing LOGIC; the
agent-loop run proves it converges in practice.

All deterministic, no LLM, no embeddings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Band boundaries (action-count thresholds). The user spec is 0-5 / 5-10 / 10-15;
# kept as the defaults, env-overridable so the boundaries can later be made
# dynamic (e.g. a fraction of the task's max_iterations) per the dynamic pillar.
EARLY_END = int(os.environ.get("GT_CURATION_EARLY_END", "5"))
MID_END = int(os.environ.get("GT_CURATION_MID_END", "10"))


def band_for(action_count: int) -> str:
    """early (< EARLY_END), mid (< MID_END), late (>=)."""
    if action_count < EARLY_END:
        return "early"
    if action_count < MID_END:
        return "mid"
    return "late"


def _norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./").lstrip("/")


@dataclass
class CurationTracker:
    """Holds the shrinking curation area for one task and narrows it per band.

    ``candidates`` is the live curation set (mutated in place by ``narrow``).
    ``neighbors_of`` maps an edited file -> its graph-connected files (callers /
    callees / contract neighbors); it is the ONLY graph dependency and is injected
    so the tracker stays pure and testable.
    """

    candidates: set[str] = field(default_factory=set)
    neighbors_of: Callable[[str], set[str]] = field(default=lambda _f: set())
    initial_size: int = field(default=0)
    last_size: int = field(default=0)

    def __post_init__(self) -> None:
        self.candidates = {_norm(c) for c in self.candidates if c}
        self.initial_size = len(self.candidates)
        self.last_size = self.initial_size

    def narrow(
        self,
        action_count: int,
        visited: Iterable[str],
        edited: Iterable[str],
    ) -> set[str]:
        """Apply the band's filter to the running curation set and return it.

        Strictly non-increasing: every branch only REMOVES from ``candidates``.
        - early: no change (full prior).
        - mid:   drop files the agent visited but did NOT edit (ruled out).
        - late:  keep only files that are edited or a neighbor of an edited file
                 (converge on the fix); if nothing has been edited yet, stay at
                 the mid-level filter rather than emptying the set.
        """
        band = band_for(action_count)
        visited_n = {_norm(v) for v in visited if v}
        edited_n = {_norm(e) for e in edited if e}
        visited_left = visited_n - edited_n  # read-and-left = ruled out

        if band == "early":
            pass  # full prior; agent is still orienting
        elif band == "mid":
            self.candidates -= visited_left
        else:  # late
            if edited_n:
                keep = set(edited_n)
                for e in edited_n:
                    try:
                        keep |= {_norm(n) for n in self.neighbors_of(e) if n}
                    except Exception:  # noqa: BLE001  — never let a provider crash narrowing
                        pass
                # Converge on the edit-neighborhood. Intersect with the running
                # set to keep shrinking — BUT if the agent edited a file the prior
                # set never contained (it localized OUTSIDE the brief — the common
                # case), the edit-neighborhood IS the new curation area; never
                # collapse to empty (an empty set would falsify the convergence
                # proof at the exact moment the agent found the fix).
                intersected = self.candidates & keep
                self.candidates = intersected if intersected else keep
            else:
                self.candidates -= visited_left
        self.last_size = len(self.candidates)
        return set(self.candidates)

    def log_line(self, action_count: int, reason: str = "") -> str:
        """Structured, hidden-prefix proof line (one per narrowing step)."""
        band = band_for(action_count)
        dropped = self.initial_size - self.last_size
        if not reason:
            reason = {"early": "early_full", "mid": "mid_drop_visited", "late": "late_edit_focus"}[band]
        return (
            f"[GT_CURATION] action={action_count} band={band} "
            f"size={self.last_size} initial={self.initial_size} "
            f"dropped={dropped} reason={reason}"
        )


__all__ = ["CurationTracker", "band_for", "EARLY_END", "MID_END"]
