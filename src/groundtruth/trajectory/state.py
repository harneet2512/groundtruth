"""DEPRECATED location for L5 trajectory state.

The canonical implementation now lives in
:mod:`groundtruth.state.agent_state` as part of FINAL_ARCH_V2 Layer 2
(see ``DECISIONS.md`` section ``## FINAL_ARCH_V2``).

This module is preserved as a re-export shim so existing imports
(`from groundtruth.trajectory.state import L5TrajectoryState`) keep working
during the migration. New code should import directly from
:mod:`groundtruth.state` (or its sub-module ``agent_state``).
"""

from __future__ import annotations

from groundtruth.state.agent_state import (
    AgentPhase,
    FailureSnapshot,
    IterationBand,
    L5TrajectoryState,
    _l5_state_path,
    _state_path,
    compute_band,
)

__all__ = [
    "AgentPhase",
    "FailureSnapshot",
    "IterationBand",
    "L5TrajectoryState",
    "_l5_state_path",
    "_state_path",
    "compute_band",
]
