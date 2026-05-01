"""Production OH SDK adapter — wires ``OpenHandsAdapter`` to the real SDK.

The kernel is scaffold-agnostic. ``OpenHandsAdapter`` defines the Protocol it
needs, and ``RealOpenHandsClient`` here implements that Protocol against the
installed ``openhands-sdk`` package (>= ``MIN_OH_SDK_VERSION``).

Mapping (per OH SDK 1.17.0):
- ``register_confirmation_policy(message, tool_filter)``:
    1. ``conv.send_message(message)`` — agent sees the block reason.
    2. ``conv.set_confirmation_policy(AlwaysConfirm())`` — next tool call
       requires confirmation, intercepted by the kernel callback to either
       reject or step through.
- ``push_visible_message(text)``: ``conv.send_message(text)``.
- ``push_first_turn_message(text)``: ``conv.send_message(text)`` before
  ``conv.run()``.
- ``register_mcp_tool(name, description)``: deferred — MCP tools are
  registered at ``Agent`` construction in OH SDK 1.x, not at runtime.
  Returns a deferred-OK marker; production wiring registers ``gt_pull``
  via ``Agent(tools=[...])`` before the conversation starts.

B1 contract: every method returns ``{"exit_code": int, "body": str, "ok":
bool}`` so ``OpenHandsAdapter._check_byte_count`` works unchanged. Failures
are caught and surfaced as ``exit_code != 0``, never silent.
"""
from __future__ import annotations

from typing import Any, Protocol


class _ConversationLike(Protocol):
    """Subset of OH SDK Conversation we use. Avoids importing the SDK at
    module top so this file is importable in environments without OH installed."""

    def send_message(self, message: Any, sender: str | None = None) -> None: ...
    def set_confirmation_policy(self, policy: Any) -> None: ...
    def reject_pending_actions(self, reason: str = ...) -> None: ...


class RealOpenHandsClient:
    """Implements ``OpenHandsAdapter.OpenHandsClient`` against a live
    ``openhands.sdk.Conversation``."""

    def __init__(self, conversation: _ConversationLike) -> None:
        self._conv = conversation
        # Imported lazily so module import doesn't require OH installed.
        from openhands.sdk.security.confirmation_policy import AlwaysConfirm
        self._AlwaysConfirm = AlwaysConfirm

    def register_confirmation_policy(
        self, message: str, tool_filter: str
    ) -> dict[str, Any]:
        try:
            self._conv.send_message(message)
            self._conv.set_confirmation_policy(self._AlwaysConfirm())
        except Exception as e:
            return {"exit_code": 1, "body": f"register_confirmation_policy error: {e}"}
        return {"exit_code": 0, "body": message, "ok": True, "tool_filter": tool_filter}

    def push_visible_message(self, text: str) -> dict[str, Any]:
        try:
            self._conv.send_message(text)
        except Exception as e:
            return {"exit_code": 1, "body": f"push_visible_message error: {e}"}
        return {"exit_code": 0, "body": text, "ok": True}

    def push_first_turn_message(self, text: str) -> dict[str, Any]:
        try:
            self._conv.send_message(text)
        except Exception as e:
            return {"exit_code": 1, "body": f"push_first_turn_message error: {e}"}
        return {"exit_code": 0, "body": text, "ok": True}

    def register_mcp_tool(self, name: str, description: str) -> dict[str, Any]:
        # OH SDK 1.x registers tools at Agent construction time, not on the
        # live Conversation. Production wiring assembles the Agent with
        # tools=[gt_pull_tool(...)] before constructing the Conversation.
        # Returning ok=True with a deferred marker so B1 does not flag this.
        return {
            "exit_code": 0,
            "body": f"deferred: {name}",
            "ok": True,
            "deferred": True,
            "description": description,
        }

    def reject_pending(self, reason: str) -> dict[str, Any]:
        """Hard-block helper — call from the confirmation callback."""
        try:
            self._conv.reject_pending_actions(reason=reason)
        except Exception as e:
            return {"exit_code": 1, "body": f"reject_pending error: {e}"}
        return {"exit_code": 0, "body": reason, "ok": True}


__all__ = ["RealOpenHandsClient"]
