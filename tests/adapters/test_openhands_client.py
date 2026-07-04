"""Tests for the production ``RealOpenHandsClient`` wired against the real
``openhands-sdk`` package. Skipped when OH is not installed."""

from __future__ import annotations

import pytest

oh_sdk = pytest.importorskip("openhands.sdk")
from openhands.sdk.security.confirmation_policy import (  # noqa: E402
    AlwaysConfirm,
    NeverConfirm,
)

from groundtruth.adapters.openhands_client import RealOpenHandsClient  # noqa: E402


class _FakeConv:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.policy = NeverConfirm()
        self.raise_on: str | None = None

    def send_message(self, message: object, sender: str | None = None) -> None:
        if self.raise_on == "send":
            raise RuntimeError("simulated send failure")
        self.calls.append(("send_message", message))

    def set_confirmation_policy(self, policy: object) -> None:
        if self.raise_on == "policy":
            raise RuntimeError("simulated policy failure")
        self.calls.append(("set_confirmation_policy", policy))
        self.policy = policy

    def reject_pending_actions(self, reason: str = "User rejected the action") -> None:
        if self.raise_on == "reject":
            raise RuntimeError("simulated reject failure")
        self.calls.append(("reject_pending_actions", reason))


def test_register_confirmation_policy_uses_real_always_confirm() -> None:
    conv = _FakeConv()
    client = RealOpenHandsClient(conv)
    res = client.register_confirmation_policy(message="block reason", tool_filter="edit")
    assert res["exit_code"] == 0
    assert res["body"] == "block reason"
    # Two real calls landed on the conversation, in order.
    assert [c[0] for c in conv.calls] == ["send_message", "set_confirmation_policy"]
    # The policy installed is the real OH SDK AlwaysConfirm class.
    assert isinstance(conv.calls[1][1], AlwaysConfirm)


def test_push_visible_message_sends_via_send_message() -> None:
    conv = _FakeConv()
    client = RealOpenHandsClient(conv)
    res = client.push_visible_message("hello")
    assert res == {"exit_code": 0, "body": "hello", "ok": True}
    assert conv.calls == [("send_message", "hello")]


def test_push_first_turn_message_sends_via_send_message() -> None:
    conv = _FakeConv()
    client = RealOpenHandsClient(conv)
    res = client.push_first_turn_message("brief text")
    assert res == {"exit_code": 0, "body": "brief text", "ok": True}
    assert conv.calls == [("send_message", "brief text")]


def test_register_mcp_tool_returns_deferred_marker() -> None:
    """OH SDK 1.x registers MCP tools at Agent build time, not at runtime.
    Production wiring must do this through Agent(tools=[...]); the client
    surfaces a deferred-OK marker so the adapter's B1 byte-count gate is
    not tripped."""
    conv = _FakeConv()
    client = RealOpenHandsClient(conv)
    res = client.register_mcp_tool("gt_pull", "graph pull")
    assert res["exit_code"] == 0
    assert res["deferred"] is True
    assert res["body"].startswith("deferred:")
    # No real calls hit the conversation.
    assert conv.calls == []


def test_send_failure_returned_as_nonzero_exit_code() -> None:
    """B1: failure must surface as exit_code != 0, never silent ok."""
    conv = _FakeConv()
    conv.raise_on = "send"
    client = RealOpenHandsClient(conv)
    res = client.push_visible_message("x")
    assert res["exit_code"] == 1
    assert "simulated send failure" in res["body"]


def test_policy_failure_during_block_returned_as_nonzero() -> None:
    conv = _FakeConv()
    conv.raise_on = "policy"
    client = RealOpenHandsClient(conv)
    res = client.register_confirmation_policy("m", "edit")
    assert res["exit_code"] == 1
    # send_message ran first, the policy call failed.
    assert conv.calls == [("send_message", "m")]


def test_reject_pending_calls_real_method() -> None:
    conv = _FakeConv()
    client = RealOpenHandsClient(conv)
    res = client.reject_pending("not focused")
    assert res == {"exit_code": 0, "body": "not focused", "ok": True}
    assert conv.calls == [("reject_pending_actions", "not focused")]
