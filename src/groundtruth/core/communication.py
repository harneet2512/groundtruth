"""Deterministic communication state machine for tool response framing.

Tracks agent session state (phase, loop detection) and provides contextual
framing text to prepend to tool responses. Pure lookup table -- no LLM calls,
no probabilistic logic.

Session state is in-memory only — resets if the MCP process restarts.
This is acceptable because the MCP spec keeps the process alive per session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskPhase(str, Enum):
    """Where the agent is in its task lifecycle."""

    EXPLORING = "exploring"
    EDITING = "editing"
    PATCH_EXISTS = "patch_exists"
    TESTED = "tested"
    SUBMITTING = "submitting"


class LoopState(str, Enum):
    """Whether the agent is stuck in a problematic pattern."""

    NORMAL = "normal"
    SEARCH_SPINNING = "search_spinning"
    CHECK_LOOPING = "check_looping"


# Tools that count as "search" for spin detection (canonical names after normalization).
_SEARCH_TOOLS = frozenset({"search", "references", "find_relevant", "trace", "impact"})

# Tools that indicate a check/validation pass.
_CHECK_TOOLS = frozenset({"check-diff", "check_patch", "check", "validate"})

# Tools that indicate testing.
_TEST_TOOLS = frozenset({"test", "run_tests"})


def normalize_tool_name(raw: str) -> str:
    """Strip MCP prefixes to get canonical tool name.

    Examples:
        "groundtruth_find_relevant" → "find_relevant"
        "groundtruth_consolidated_check" → "check"
        "groundtruth_impact" → "impact"
        "impact" → "impact" (already canonical)
    """
    name = raw.removeprefix("groundtruth_")
    name = name.removeprefix("consolidated_")
    return name


@dataclass(frozen=True)
class SessionState:
    """Immutable snapshot of agent session progress."""

    phase: TaskPhase = TaskPhase.EXPLORING
    tools_called: dict[str, int] = field(default_factory=dict)
    last_tool: str | None = None
    edits_made: int = 0
    checks_run: int = 0

    def _copy(self, **overrides: object) -> SessionState:
        """Create a new state with selected fields overridden."""
        defaults: dict[str, object] = {
            "phase": self.phase,
            "tools_called": dict(self.tools_called),
            "last_tool": self.last_tool,
            "edits_made": self.edits_made,
            "checks_run": self.checks_run,
        }
        defaults.update(overrides)
        return SessionState(**defaults)  # type: ignore[arg-type]


class CommunicationPolicy:
    """Deterministic framing policy based on session state.

    Provides optional framing text to prepend to tool responses, and detects
    problematic agent loops (search spinning, check looping).
    """

    def __init__(
        self,
        search_spin_threshold: int = 3,
        check_loop_threshold: int = 2,
    ) -> None:
        self.search_spin_threshold = search_spin_threshold
        self.check_loop_threshold = check_loop_threshold

    def record_tool_call(
        self,
        state: SessionState,
        tool_name: str,
        evidence: dict[str, object] | None = None,
    ) -> SessionState:
        """Return new state after a tool call.

        Args:
            state: Current session state.
            tool_name: Canonical tool name (use normalize_tool_name() first).
            evidence: Optional evidence dict from the handler. Used for
                evidence-based transitions (e.g., has_changes for PATCH_EXISTS).
        """
        new_counts = dict(state.tools_called)
        new_counts[tool_name] = new_counts.get(tool_name, 0) + 1

        new_checks = state.checks_run + (1 if tool_name in _CHECK_TOOLS else 0)

        # Phase transitions — evidence-based where possible.
        new_phase = state.phase
        if tool_name in _CHECK_TOOLS and state.phase in (
            TaskPhase.EDITING,
            TaskPhase.EXPLORING,
        ):
            # Only transition to PATCH_EXISTS if evidence confirms changes exist.
            # Empty diffs, reverted diffs, no-op diffs must NOT trigger transition.
            if evidence and evidence.get("has_changes"):
                new_phase = TaskPhase.PATCH_EXISTS
        elif tool_name in _TEST_TOOLS and state.phase == TaskPhase.PATCH_EXISTS:
            new_phase = TaskPhase.TESTED

        return state._copy(
            tools_called=new_counts,
            last_tool=tool_name,
            checks_run=new_checks,
            phase=new_phase,
        )

    def record_edit(self, state: SessionState) -> SessionState:
        """Return new state after an edit is detected."""
        new_phase = TaskPhase.EDITING if state.phase == TaskPhase.EXPLORING else state.phase
        return state._copy(edits_made=state.edits_made + 1, phase=new_phase)

    def detect_loop(self, state: SessionState) -> LoopState:
        """Detect if agent is stuck in a problematic loop."""
        search_count = sum(state.tools_called.get(t, 0) for t in _SEARCH_TOOLS)
        if search_count >= self.search_spin_threshold and state.edits_made == 0:
            return LoopState.SEARCH_SPINNING

        if state.checks_run >= self.check_loop_threshold and state.phase in (
            TaskPhase.PATCH_EXISTS,
            TaskPhase.TESTED,
        ):
            return LoopState.CHECK_LOOPING

        return LoopState.NORMAL

    def get_framing(self, state: SessionState, tool_name: str) -> str | None:
        """Get optional framing text to prepend to a tool response.

        Returns None when no special framing is needed.
        """
        loop = self.detect_loop(state)

        # Loop-based framing takes priority.
        if loop == LoopState.SEARCH_SPINNING:
            return "Multiple searches without editing. Try impact on your target symbol."
        if loop == LoopState.CHECK_LOOPING and tool_name in _CHECK_TOOLS:
            return "Check already run. Do not revise [INFO] findings. Submit."

        # Phase + tool framing.
        if tool_name == "impact":
            if state.phase == TaskPhase.EXPLORING:
                return "\u2192 Edit these methods, then run check once."
            if state.phase == TaskPhase.PATCH_EXISTS:
                return "\u2192 Patch exists. Verify obligations are covered."

        if tool_name == "references" and state.phase == TaskPhase.PATCH_EXISTS:
            return "\u2192 Patch exists. Consider running check-diff."

        return None
