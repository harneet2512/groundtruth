"""Layer 3 — Collaboration Router (FINAL_ARCH_V2 §3 Layer 3).

The router observes the agent trajectory (Layer 2 ``AgentState``), asks Layer 4
providers for evidence, and decides:

- WHEN to emit (or suppress, with a reason)
- WHICH provider's output to surface
- WHICH primary edge to render

Router rules:
- Router decides timing. It does NOT compute evidence.
- Router suppression reasons are first-class (see ``SuppressionReason``) so the
  shadow-replay metric script can attribute every non-emission.
- Router enforces per-task injection budget + per-event debounce per
  Decision 34 §12.
- Router is invoked by the wrapper at FileRead/FileEdit boundaries. The
  wrapper does NOT currently route through this module — the router is in
  shadow mode and is exercised only by ``scripts/shadow_replay.py`` until
  agent-visible behavior is flagged on.
"""

from groundtruth.router.decisions import (
    EmissionKind,
    RouterEmission,
    SuppressionReason,
)
from groundtruth.router.router import CollaborationRouter

__all__ = [
    "CollaborationRouter",
    "EmissionKind",
    "RouterEmission",
    "SuppressionReason",
]
