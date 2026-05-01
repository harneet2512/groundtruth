"""Adapter base contract.

Adapters translate scaffold-specific events into kernel-canonical types and
translate kernel decisions back into scaffold actions. They contain no GT
decision logic. The kernel never imports adapters; adapters import the kernel.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from groundtruth.control.paths import normalize as _norm_path
from groundtruth.control.types import (
    BriefResult,
    Capabilities,
    Decision,
    DecisionAction,
    EditEvent,
    PullQuery,
)

_FILE_MENTION_RE = re.compile(
    r"\b[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|c|h|cc|cpp|hpp|rb|php|cs|md|rst|toml|json|yaml|yml)\b"
)


def _sanitize_line(line: str, allowed: set[str]) -> str:
    """Boundary 3 path-stripping. Inlined to keep adapters/ free of
    pretask/runtime imports — the adapter must remain installable on its own."""
    text = str(line)
    for match in _FILE_MENTION_RE.findall(text):
        if "*" in match or "?" in match:
            continue
        if _norm_path(match) in allowed:
            continue
        escaped = re.escape(match)
        text = re.sub(rf"\s*\bfrom\s+{escaped}\b\s*:?\s*", " ", text)
        text = re.sub(rf"\s*\bin\s+{escaped}\b\s*:?\s*", " ", text)
        text = re.sub(rf"\s*\bat\s+{escaped}\b\s*:?\s*", " ", text)
        text = re.sub(rf"\b{escaped}\b\s*:\s*", "", text)
        text = re.sub(rf"\b{escaped}\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_render(text: str, allowed_paths: set[str]) -> str:
    """Boundary 3 -- strip non-allowed file paths from agent-facing text.

    Adapters MUST call this before returning a ``ScaffoldArtifact`` so paths
    from internal sources (telemetry-only modules, render-layer artifacts)
    cannot smuggle into the agent context.

    ``allowed_paths`` is treated as a normalized set; the helper normalizes
    callers' input via ``control.paths.normalize`` so callers can pass
    ``brief.focus_files`` directly without pre-normalizing.
    """
    normalized = {_norm_path(str(p)) for p in allowed_paths}
    return "\n".join(_sanitize_line(line, normalized) for line in text.splitlines())


@dataclass(frozen=True)
class DegradeMap:
    block_to: DecisionAction = DecisionAction.VISIBLE
    visible_to: DecisionAction = DecisionAction.AUDIT
    replan_inject_to: DecisionAction = DecisionAction.VISIBLE


@dataclass
class AppliedDecision:
    actual_action: DecisionAction
    delivered: bool
    degraded_from: DecisionAction | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScaffoldArtifact:
    kind: str
    payload: dict[str, Any]


class Adapter(ABC):
    """Per-scaffold translation surface. Subclasses must declare capabilities."""

    name: str
    capabilities: Capabilities
    degrade_map: DegradeMap = DegradeMap()

    def __init__(self) -> None:
        if not isinstance(getattr(self, "capabilities", None), Capabilities):
            raise TypeError(f"{type(self).__name__} must declare 'capabilities: Capabilities'")
        if not getattr(self, "name", None):
            raise TypeError(f"{type(self).__name__} must declare a non-empty 'name'")
        if not self.capabilities.audit:
            # Audit-only logging is the floor; every adapter must support it.
            raise ValueError(f"{self.name}: 'audit' capability is required")

    def degrade(self, action: DecisionAction) -> DecisionAction:
        if action == DecisionAction.BLOCK and not self.capabilities.block:
            return self.degrade_map.block_to
        if action == DecisionAction.VISIBLE and not self.capabilities.visible:
            return self.degrade_map.visible_to
        return action

    @abstractmethod
    def render_brief(self, brief: BriefResult) -> ScaffoldArtifact: ...

    @abstractmethod
    def apply_decision(self, decision: Decision) -> AppliedDecision: ...

    @abstractmethod
    def parse_edit(self, scaffold_event: Any) -> EditEvent: ...

    @abstractmethod
    def route_pull(self, scaffold_tool_call: Any) -> PullQuery: ...
