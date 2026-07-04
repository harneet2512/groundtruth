"""GroundTruth Layer 2 — Agent-State Tracker.

See ``DECISIONS.md`` section ``## FINAL_ARCH_V2`` §3 Layer 2.

This package is the canonical home for every fact GT knows about the agent's
trajectory: what files it viewed, what it edited, what it searched, where its
current focus is, which GT suggestions are still pending vs ignored, and which
iteration band it is in.

Historically these facts lived in five places:

- ``/tmp/gt_viewed.txt`` / ``/tmp/gt_brief_candidates.txt`` / ``/tmp/gt_issue_terms.txt``
  (read by ``src/groundtruth/hooks/post_view.py``)
- ``GTRuntimeConfig._pending_next_actions`` (in the OH wrapper)
- ``src/groundtruth/trajectory/state.py`` (L5TrajectoryState)
- Ad-hoc fields scattered across ``GTRuntimeConfig`` (action_count, max_iter,
  brief_candidates, _l3_fire_count, ...)
- ``scripts/localization_metrics.py`` reconstructing some of this from output.jsonl

FINAL_ARCH_V2 consolidates them under one ``AgentState`` object.

Backwards compatibility: the legacy tmp files and the existing ``L5TrajectoryState``
JSON sidecar are still written so subprocess hooks and offline metrics scripts
keep working unchanged. They will be removed only once every callsite reads
from ``AgentState`` directly.
"""

from groundtruth.state.agent_state import (
    AgentPhase,
    AgentState,
    FailureSnapshot,
    IterationBand,
    PendingSuggestion,
    SearchEvent,
    ViewedFile,
    canonical_repo_path,
    compute_band,
    L5TrajectoryState,
)

__all__ = [
    "AgentPhase",
    "AgentState",
    "FailureSnapshot",
    "IterationBand",
    "L5TrajectoryState",
    "PendingSuggestion",
    "SearchEvent",
    "ViewedFile",
    "canonical_repo_path",
    "compute_band",
]
