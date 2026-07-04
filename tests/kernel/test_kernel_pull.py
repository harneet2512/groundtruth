"""Stress tests for ``kernel.pull`` MCP routing + Boundary 1 whitelist.

Mocks ``mcp.tools.handle_*`` because those have their own tests; we test
the routing + whitelist + evidence-assembly contract here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from groundtruth.control import kernel
from groundtruth.control.types import (
    BriefResult,
    Capabilities,
    PullKind,
    PullQuery,
    PullResult,
    RunState,
)


def _rs() -> RunState:
    return RunState(
        task_id="t1",
        plan={},
        brief_result=None,
        capabilities=Capabilities(
            block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True
        ),
    )


def _trace_payload() -> dict:
    return {
        "symbol": {"name": "User.has_perm", "file": "django/contrib/auth/models.py", "node_id": 4421},
        "callers": [
            {"qualified_name": "django.views.auth.login_required", "node_id": 9001},
            {"qualified_name": "django.contrib.admin.options.has_change_permission", "node_id": 9002},
        ],
        "callees": [],
        "reasoning_guidance": "INTERNAL: do not surface this string",
        "intervention_id": "internal-uuid-deadbeef",
    }


# happy
def test_happy_trace_payload_routed() -> None:
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(
            PullQuery(kind=PullKind.TRACE, args={"symbol": "User.has_perm"}),
            _rs(),
        )
    assert isinstance(result, PullResult)
    assert result.kind == PullKind.TRACE
    assert "callers" in result.payload
    assert len(result.payload["callers"]) == 2


# boundary -- evidence assembly
def test_boundary_evidence_node_ids_collected_from_callers() -> None:
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(PullQuery(kind=PullKind.TRACE, args={"symbol": "x"}), _rs())
    assert 9001 in result.evidence.node_ids
    assert 9002 in result.evidence.node_ids
    assert 4421 in result.evidence.node_ids


def test_boundary_telemetry_record_id_present() -> None:
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(PullQuery(kind=PullKind.TRACE, args={"symbol": "x"}), _rs())
    assert result.telemetry_record_id
    assert len(result.telemetry_record_id) >= 8


# adversarial -- the leakage cases
def test_adversarial_reasoning_guidance_dropped() -> None:
    """`reasoning_guidance` is internal narrative -- must not cross Boundary 1."""
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(PullQuery(kind=PullKind.TRACE, args={"symbol": "x"}), _rs())
    assert "reasoning_guidance" not in result.payload


def test_adversarial_intervention_id_dropped() -> None:
    """`intervention_id` is tracker-internal -- must not cross Boundary 1."""
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(PullQuery(kind=PullKind.TRACE, args={"symbol": "x"}), _rs())
    assert "intervention_id" not in result.payload


def test_adversarial_unknown_kind_does_not_crash() -> None:
    """An unknown handler returns error payload, not exception."""
    # PullKind enum doesn't have UNKNOWN -- use VALIDATE which exists but
    # mock the handler missing.
    with patch("groundtruth.mcp.tools.handle_validate", side_effect=AttributeError):
        # Patch getattr behaviour by making the handler attribute absent.
        import groundtruth.mcp.tools as mcp_tools
        original = getattr(mcp_tools, "handle_validate", None)
        try:
            del mcp_tools.handle_validate  # type: ignore[attr-defined]
        except AttributeError:
            pass
        try:
            result = kernel.pull(
                PullQuery(kind=PullKind.VALIDATE, args={"proposed_code": "x", "file_path": "a.py"}),
                _rs(),
            )
            assert "_error" in result.payload
        finally:
            if original is not None:
                mcp_tools.handle_validate = original  # type: ignore[attr-defined]


# mutation pin -- if PULL_WHITELIST drops the "callers" key, this fails
def test_mutation_pin_callers_in_whitelist() -> None:
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(PullQuery(kind=PullKind.TRACE, args={"symbol": "x"}), _rs())
    assert "callers" in result.payload  # If whitelist regresses this fails.


# mutation pin -- if PullKind enum value is wrong this fails
def test_mutation_pin_pull_kind_round_trip() -> None:
    async def fake_handle_trace(**_kwargs):
        return _trace_payload()

    with patch("groundtruth.mcp.tools.handle_trace", new=fake_handle_trace):
        result = kernel.pull(PullQuery(kind=PullKind.TRACE, args={"symbol": "x"}), _rs())
    assert result.kind.value == "trace"
