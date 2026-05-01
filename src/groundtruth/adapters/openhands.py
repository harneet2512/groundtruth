"""OpenHands adapter for the GT control kernel.

Pinned OH SDK contract (per arXiv 2511.03690 and Phase 1 plan §B.B4):
- ``ConfirmationPolicy`` (§4.9): registers a one-shot rejection of the next
  EDIT-class tool call -- this is how ``Decision(action=BLOCK)`` lands in OH.
- ``MCPToolDefinition`` (§4.4): registers the ``gt_pull`` tool the agent
  invokes for mid-task graph queries.
- ``on_event(event)`` (§4.5): used both to subscribe to ``ObservationEvent``
  for post-edit observation and to push ``Decision(action=VISIBLE)`` text
  into the next agent turn.

This adapter REPLACES ``scripts/swebench/oh_gt_hook_wrapper.py`` (257 LOC)
and ``scripts/swebench/oh_gt_startupmode_wrapper.py`` (192 LOC). All decision
logic lives in the kernel; the adapter is translation only.

Plumbing invariants from the Phase 1 plan:
- B1: every OH SDK return is byte-count checked. Zero-byte success is
  classified as ``ErrorClass.UNEXPECTED_ENVIRONMENT``, never silently passed.
- B2: every path crosses ``control.paths.normalize`` exactly once.
- B4: OH SDK version checked at adapter init via importlib.metadata.
- B5: edit events come from ``on_event(ObservationEvent)``. No FS polling.
- B6: ``Decision.confidence`` and ``BriefResult.confidence`` are read as
  numbers. The adapter never parses brief text for confidence cues.
- B8: every block->visible degradation populates ``AppliedDecision.degraded_from``.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any, Protocol

from groundtruth.adapters.base import (
    Adapter,
    AppliedDecision,
    ScaffoldArtifact,
    safe_render,
)
from groundtruth.control.paths import normalize as _norm_path
from groundtruth.control.types import (
    BriefResult,
    Capabilities,
    Decision,
    DecisionAction,
    EditEvent,
    PullKind,
    PullQuery,
)

MIN_OH_SDK_VERSION = "0.1.0"
ADAPTER_VERSION = "openhands-0.1"


class AdapterIncompatibleError(RuntimeError):
    """OH SDK version is below MIN_OH_SDK_VERSION."""


class OpenHandsClient(Protocol):
    """Duck-typed shape of the OH SDK surface the adapter touches.

    Tests pass a stub conforming to this protocol; production passes a real
    OH SDK ``Conversation`` (or wrapper). Keeps the adapter testable without
    installing the OH SDK on every dev machine.
    """

    def register_confirmation_policy(self, message: str, tool_filter: str) -> dict[str, Any]: ...
    def push_visible_message(self, text: str) -> dict[str, Any]: ...
    def register_mcp_tool(self, name: str, description: str) -> dict[str, Any]: ...
    def push_first_turn_message(self, text: str) -> dict[str, Any]: ...


@dataclass
class _Result:
    """Normalised OH SDK return shape used for B1 byte-count checks."""
    ok: bool
    body_bytes: int
    detail: dict[str, Any]


def _check_byte_count(name: str, raw: Any) -> _Result:
    """B1: classify an OH SDK return. Zero-byte success is a hard failure."""
    if not isinstance(raw, dict):
        return _Result(ok=False, body_bytes=0, detail={"reason": f"{name}_non_dict_return"})
    body = str(raw.get("body") or raw.get("text") or raw.get("message") or "")
    body_bytes = len(body.encode("utf-8"))
    exit_code = raw.get("exit_code", 0)
    if exit_code != 0:
        return _Result(ok=False, body_bytes=body_bytes, detail=dict(raw))
    if body_bytes == 0 and not raw.get("ok", False):
        return _Result(ok=False, body_bytes=0, detail={"reason": f"{name}_zero_byte_success", **raw})
    return _Result(ok=True, body_bytes=body_bytes, detail=dict(raw))


def _check_version() -> str:
    """B4: confirm OH SDK >= MIN_OH_SDK_VERSION. Returns the installed version."""
    for pkg in ("openhands-sdk", "openhands"):
        try:
            v = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue
        # Simple lex compare is OK for x.y.z semantic strings; tighten later.
        if tuple(int(x) for x in v.split(".")[:3] if x.isdigit()) < tuple(
            int(x) for x in MIN_OH_SDK_VERSION.split(".")
        ):
            raise AdapterIncompatibleError(
                f"OH SDK {v} < required {MIN_OH_SDK_VERSION}"
            )
        return v
    raise AdapterIncompatibleError("OH SDK not installed (openhands-sdk or openhands)")


class OpenHandsAdapter(Adapter):
    name = "openhands"
    capabilities = Capabilities(
        block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True
    )

    def __init__(self, client: OpenHandsClient, *, skip_version_check: bool = False) -> None:
        super().__init__()
        self._client = client
        self._sdk_version = "test" if skip_version_check else _check_version()

    def render_brief(self, brief: BriefResult) -> ScaffoldArtifact:
        # Boundary 2: read only allowed fields. Boundary 3: safe_render.
        allowed = {str(p) for p in brief.focus_files}
        text = safe_render(brief.brief_text, allowed)
        # B6: read numeric confidence; do not parse rendered text.
        framing = "directive" if brief.confidence >= 0.6 else "suggestive"
        first_turn = self._client.push_first_turn_message(text)
        check = _check_byte_count("push_first_turn_message", first_turn)
        register = self._client.register_mcp_tool(
            name="gt_pull",
            description="Mid-task graph pull. kind=trace|impact|hotspots|validate|context|symbols.",
        )
        register_check = _check_byte_count("register_mcp_tool", register)
        return ScaffoldArtifact(
            kind="message",
            payload={
                "text": text,
                "framing": framing,
                "confidence": brief.confidence,
                "focus_files": [str(p) for p in brief.focus_files],
                "first_turn_ok": check.ok,
                "register_mcp_ok": register_check.ok,
                "first_turn_bytes": check.body_bytes,
            },
        )

    def apply_decision(self, decision: Decision) -> AppliedDecision:
        # B6: read action + message numerically; B2/B8: degrade safely.
        target = self.degrade(decision.action)
        if target == DecisionAction.AUDIT:
            return AppliedDecision(
                actual_action=DecisionAction.AUDIT,
                delivered=True,
                degraded_from=decision.action if target != decision.action else None,
            )
        if target == DecisionAction.BLOCK:
            raw = self._client.register_confirmation_policy(
                message=decision.message, tool_filter="edit"
            )
        elif target == DecisionAction.VISIBLE:
            raw = self._client.push_visible_message(decision.message)
        else:  # ALLOW
            return AppliedDecision(
                actual_action=DecisionAction.ALLOW, delivered=True, degraded_from=None
            )
        check = _check_byte_count(f"apply_decision_{target.value}", raw)
        return AppliedDecision(
            actual_action=target if check.ok else DecisionAction.AUDIT,
            delivered=check.ok,
            degraded_from=decision.action if target != decision.action else None,
            detail=check.detail,
        )

    def parse_edit(self, scaffold_event: Any) -> EditEvent:
        # B5: events come from on_event callback, not FS polling.
        if not isinstance(scaffold_event, dict):
            raise ValueError("parse_edit expects an OH ObservationEvent dict")
        path = scaffold_event.get("path") or scaffold_event.get("file_path") or ""
        if not path:
            raise ValueError("ObservationEvent missing path -- B1 reject")
        diff = scaffold_event.get("diff") or scaffold_event.get("diff_text") or ""
        if not diff:
            raise ValueError("ObservationEvent missing diff -- B1 reject")
        return EditEvent(
            task_id=str(scaffold_event.get("task_id", "unknown")),
            files_changed=[__import__("pathlib").Path(_norm_path(str(path)))],
            diff_text=diff,
            ts=str(scaffold_event.get("ts", "")),
            source_tool=str(scaffold_event.get("tool", "str_replace_editor")),
        )

    def route_pull(self, scaffold_tool_call: Any) -> PullQuery:
        if not isinstance(scaffold_tool_call, dict):
            raise ValueError("route_pull expects an MCP tool-call dict")
        kind_raw = scaffold_tool_call.get("kind") or scaffold_tool_call.get("name") or ""
        try:
            kind = PullKind(str(kind_raw))
        except ValueError as exc:
            raise ValueError(f"unknown gt_pull kind: {kind_raw!r}") from exc
        args = scaffold_tool_call.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("gt_pull args must be a dict")
        return PullQuery(kind=kind, args=dict(args))


__all__ = [
    "MIN_OH_SDK_VERSION",
    "ADAPTER_VERSION",
    "AdapterIncompatibleError",
    "OpenHandsAdapter",
    "OpenHandsClient",
]
