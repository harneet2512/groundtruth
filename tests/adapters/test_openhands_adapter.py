"""Stress tests for the OpenHands adapter.

Layers per locked decision 6: happy / boundary / adversarial / mutation.
The adapter never imports the real OH SDK in tests -- ``OpenHandsClient`` is a
Protocol, so a stub satisfying the same shape passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from groundtruth.adapters.base import AppliedDecision, ScaffoldArtifact
from groundtruth.adapters.openhands import (
    MIN_OH_SDK_VERSION,
    AdapterIncompatibleError,
    OpenHandsAdapter,
)
from groundtruth.control.types import (
    BriefResult,
    Decision,
    DecisionAction,
    Evidence,
    PullKind,
)


class _StubClient:
    def __init__(self, *, return_zero_byte: bool = False, return_error: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._zero = return_zero_byte
        self._error = return_error

    def _result(self, kind: str, body: str = "ok") -> dict[str, Any]:
        if self._error:
            return {"exit_code": 1, "body": "fail"}
        if self._zero:
            return {"exit_code": 0, "body": ""}
        return {"exit_code": 0, "body": body, "kind": kind}

    def push_first_turn_message(self, text: str) -> dict[str, Any]:
        self.calls.append(("push_first_turn_message", {"text": text}))
        return self._result("first_turn", text)

    def register_mcp_tool(self, name: str, description: str) -> dict[str, Any]:
        self.calls.append(("register_mcp_tool", {"name": name, "description": description}))
        return self._result("mcp_tool", name)

    def register_confirmation_policy(self, message: str, tool_filter: str) -> dict[str, Any]:
        self.calls.append(("register_confirmation_policy", {"message": message, "filter": tool_filter}))
        return self._result("confirmation", message)

    def push_visible_message(self, text: str) -> dict[str, Any]:
        self.calls.append(("push_visible_message", {"text": text}))
        return self._result("visible", text)


def _adapter(*, client: _StubClient | None = None) -> OpenHandsAdapter:
    return OpenHandsAdapter(client or _StubClient(), skip_version_check=True)


def _brief(confidence: float = 0.7, brief_text: str = "Edit src/a.py first.") -> BriefResult:
    return BriefResult(
        brief_text=brief_text,
        candidates=[],
        focus_files=[Path("src/a.py")],
        cluster_files=[Path("src/a.py")],
        confidence=confidence,
        plan={},
        plan_path=None,
    )


def _decision(action: DecisionAction = DecisionAction.BLOCK) -> Decision:
    return Decision(
        action=action,
        severity="block",
        reasons=["first_edit_root_scaffold"],
        message="GT runtime intervention [block]\nReasons: first_edit_root_scaffold",
        evidence=Evidence(),
        confidence=0.78,
        rule_id="first_edit_root_scaffold",
        rule_version="kernel-0.1",
    )


# init / version
def test_init_succeeds_with_skip_version_check() -> None:
    a = _adapter()
    assert a.name == "openhands"
    assert a.capabilities.block is True


def test_init_raises_when_oh_sdk_missing() -> None:
    """B4: missing OH SDK -> AdapterIncompatibleError, never silent.

    Mocks importlib.metadata.version so the assertion is hermetic regardless
    of whether OH SDK is installed in the dev environment."""
    import importlib.metadata as _md

    def _missing(name: str) -> str:
        raise _md.PackageNotFoundError(name)

    with patch("importlib.metadata.version", side_effect=_missing):
        with pytest.raises(AdapterIncompatibleError):
            OpenHandsAdapter(_StubClient(), skip_version_check=False)


def test_init_raises_when_oh_sdk_too_old() -> None:
    """B4: OH SDK older than MIN_OH_SDK_VERSION -> AdapterIncompatibleError."""
    def fake_version(name: str) -> str:
        if name in ("openhands-sdk", "openhands"):
            return "0.0.1"
        raise __import__("importlib.metadata").metadata.PackageNotFoundError(name)

    with patch("importlib.metadata.version", side_effect=fake_version):
        with pytest.raises(AdapterIncompatibleError, match="<"):
            OpenHandsAdapter(_StubClient(), skip_version_check=False)


# render_brief
def test_render_brief_returns_artifact_with_text() -> None:
    a = _adapter()
    art = a.render_brief(_brief())
    assert isinstance(art, ScaffoldArtifact)
    assert "src/a.py" in art.payload["text"]
    assert art.payload["confidence"] == 0.7
    assert art.payload["framing"] == "directive"


def test_render_brief_low_confidence_uses_suggestive_framing() -> None:
    """B6: framing decided by numeric confidence, not rendered text."""
    a = _adapter()
    art = a.render_brief(_brief(confidence=0.40, brief_text="deterministic edit plan"))
    assert art.payload["framing"] == "suggestive"


def test_render_brief_safe_render_strips_disallowed_paths() -> None:
    """Boundary 3: safe_render must remove paths not in focus_files."""
    a = _adapter()
    text = "Edit src/a.py first. Also see src/secret/log.py."
    art = a.render_brief(_brief(brief_text=text))
    assert "src/a.py" in art.payload["text"]
    assert "src/secret/log.py" not in art.payload["text"]


def test_render_brief_zero_byte_response_flagged() -> None:
    """B1: zero-byte success classified as failure, not silent pass."""
    client = _StubClient(return_zero_byte=True)
    a = _adapter(client=client)
    art = a.render_brief(_brief())
    assert art.payload["first_turn_ok"] is False
    assert art.payload["first_turn_bytes"] == 0


# apply_decision
def test_apply_decision_block_via_confirmation_policy() -> None:
    client = _StubClient()
    a = _adapter(client=client)
    applied = a.apply_decision(_decision(DecisionAction.BLOCK))
    assert isinstance(applied, AppliedDecision)
    assert applied.actual_action == DecisionAction.BLOCK
    assert applied.delivered is True
    assert applied.degraded_from is None
    assert any(c[0] == "register_confirmation_policy" for c in client.calls)


def test_apply_decision_visible_via_on_event() -> None:
    client = _StubClient()
    a = _adapter(client=client)
    applied = a.apply_decision(_decision(DecisionAction.VISIBLE))
    assert applied.actual_action == DecisionAction.VISIBLE
    assert any(c[0] == "push_visible_message" for c in client.calls)


def test_apply_decision_audit_no_client_call() -> None:
    """audit means kernel.log only -- adapter does nothing on the OH side."""
    client = _StubClient()
    a = _adapter(client=client)
    applied = a.apply_decision(_decision(DecisionAction.AUDIT))
    assert applied.actual_action == DecisionAction.AUDIT
    assert client.calls == []


def test_apply_decision_zero_byte_oh_return_degrades_to_audit() -> None:
    """B1: zero-byte success on apply_decision degrades to AUDIT, never silent BLOCK."""
    client = _StubClient(return_zero_byte=True)
    a = _adapter(client=client)
    applied = a.apply_decision(_decision(DecisionAction.BLOCK))
    assert applied.delivered is False
    assert applied.actual_action == DecisionAction.AUDIT


# parse_edit
def test_parse_edit_normalizes_path() -> None:
    """B2: paths come out normalized (workspace/ stripped, leading dots preserved)."""
    a = _adapter()
    ev = a.parse_edit(
        {
            "task_id": "t1",
            "path": "/workspace/src/a.py",
            "diff": "diff --git a/src/a.py b/src/a.py\n",
            "ts": "2026-05-01T00:00:00Z",
            "tool": "str_replace_editor",
        }
    )
    assert ev.files_changed == [Path("src/a.py")]


def test_parse_edit_rejects_empty_path() -> None:
    """B1: empty-path event rejected, never silent pass."""
    a = _adapter()
    with pytest.raises(ValueError, match="path"):
        a.parse_edit({"task_id": "t1", "diff": "x", "ts": "x"})


def test_parse_edit_rejects_empty_diff() -> None:
    """B1: zero-length diff rejected (the OH wrapper 0-byte stdout pattern)."""
    a = _adapter()
    with pytest.raises(ValueError, match="diff"):
        a.parse_edit({"task_id": "t1", "path": "src/a.py", "diff": "", "ts": "x"})


# route_pull
def test_route_pull_trace() -> None:
    a = _adapter()
    q = a.route_pull({"name": "trace", "args": {"symbol": "User.has_perm"}})
    assert q.kind == PullKind.TRACE
    assert q.args == {"symbol": "User.has_perm"}


def test_route_pull_unknown_kind_raises() -> None:
    """B1: unknown pull kind rejected, not silently dropped."""
    a = _adapter()
    with pytest.raises(ValueError, match="unknown"):
        a.route_pull({"name": "made_up_tool", "args": {}})


# capability degradation pin
def test_apply_decision_degrades_when_block_not_supported() -> None:
    """B8: block -> visible degradation populates degraded_from."""

    class _NoBlockAdapter(OpenHandsAdapter):
        capabilities = OpenHandsAdapter.capabilities.model_copy(update={"block": False})

    client = _StubClient()
    a = _NoBlockAdapter(client, skip_version_check=True)
    applied = a.apply_decision(_decision(DecisionAction.BLOCK))
    assert applied.actual_action == DecisionAction.VISIBLE
    assert applied.degraded_from == DecisionAction.BLOCK
    assert any(c[0] == "push_visible_message" for c in client.calls)


# mutation pin -- if MIN_OH_SDK_VERSION is empty / non-semver this would fail
def test_mutation_pin_min_version_is_semver_compatible() -> None:
    parts = MIN_OH_SDK_VERSION.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)
