"""Tests for adapters.base -- IMPLEMENTED, not xfail.

Asserts the adapter contract: capability declarations are validated at
construction; DegradeMap returns expected fallbacks per action; AppliedDecision
populates degraded_from correctly when the adapter degrades a decision.
"""

from __future__ import annotations

from typing import Any

import pytest

from groundtruth.adapters.base import (
    Adapter,
    AppliedDecision,
    DegradeMap,
    ScaffoldArtifact,
)
from groundtruth.control.types import (
    BriefResult,
    Capabilities,
    Decision,
    DecisionAction,
    EditEvent,
    Evidence,
    PullKind,
    PullQuery,
)


class _FullCapAdapter(Adapter):
    name = "full"
    capabilities = Capabilities(block=True, visible=True, audit=True, mid_task_pull=True, replan_inject=True)

    def render_brief(self, brief: BriefResult) -> ScaffoldArtifact:
        return ScaffoldArtifact(kind="message", payload={"text": brief.brief_text})

    def apply_decision(self, decision: Decision) -> AppliedDecision:
        action = self.degrade(decision.action)
        return AppliedDecision(
            actual_action=action,
            delivered=True,
            degraded_from=decision.action if action != decision.action else None,
        )

    def parse_edit(self, scaffold_event: Any) -> EditEvent:
        return EditEvent(
            task_id=scaffold_event["task_id"],
            files_changed=scaffold_event["files"],
            diff_text=scaffold_event.get("diff", ""),
            ts=scaffold_event["ts"],
            source_tool=scaffold_event["tool"],
        )

    def route_pull(self, scaffold_tool_call: Any) -> PullQuery:
        return PullQuery(kind=PullKind.TRACE, args=scaffold_tool_call.get("args", {}))


class _NoBlockAdapter(_FullCapAdapter):
    name = "no_block"
    capabilities = Capabilities(block=False, visible=True, audit=True, mid_task_pull=False, replan_inject=False)


class _NoAuditAdapter(_FullCapAdapter):
    name = "no_audit"
    capabilities = Capabilities(block=True, visible=True, audit=False, mid_task_pull=True, replan_inject=True)


class _MissingCapsAdapter(Adapter):
    name = "broken"
    # capabilities omitted on purpose

    def render_brief(self, brief: BriefResult) -> ScaffoldArtifact: ...
    def apply_decision(self, decision: Decision) -> AppliedDecision: ...
    def parse_edit(self, scaffold_event: Any) -> EditEvent: ...
    def route_pull(self, scaffold_tool_call: Any) -> PullQuery: ...


def _make_decision(action: DecisionAction) -> Decision:
    return Decision(
        action=action,
        severity=action.value,
        reasons=["test"],
        message="test",
        evidence=Evidence(),
        confidence=0.7,
        rule_id="test_rule",
        rule_version="kernel-0.1",
    )


def test_full_capability_adapter_constructs() -> None:
    adapter = _FullCapAdapter()
    assert adapter.name == "full"
    assert adapter.capabilities.block is True


def test_missing_capabilities_attr_raises() -> None:
    with pytest.raises(TypeError, match="capabilities"):
        _MissingCapsAdapter()


def test_audit_capability_required() -> None:
    with pytest.raises(ValueError, match="audit"):
        _NoAuditAdapter()


def test_degrade_block_to_visible_when_no_block() -> None:
    adapter = _NoBlockAdapter()
    assert adapter.degrade(DecisionAction.BLOCK) == DecisionAction.VISIBLE


def test_degrade_passthrough_when_capable() -> None:
    adapter = _FullCapAdapter()
    assert adapter.degrade(DecisionAction.BLOCK) == DecisionAction.BLOCK
    assert adapter.degrade(DecisionAction.VISIBLE) == DecisionAction.VISIBLE


def test_apply_decision_records_degraded_from() -> None:
    adapter = _NoBlockAdapter()
    applied = adapter.apply_decision(_make_decision(DecisionAction.BLOCK))
    assert applied.actual_action == DecisionAction.VISIBLE
    assert applied.degraded_from == DecisionAction.BLOCK


def test_apply_decision_no_degradation_when_capable() -> None:
    adapter = _FullCapAdapter()
    applied = adapter.apply_decision(_make_decision(DecisionAction.BLOCK))
    assert applied.actual_action == DecisionAction.BLOCK
    assert applied.degraded_from is None


def test_default_degrade_map_values() -> None:
    dm = DegradeMap()
    assert dm.block_to == DecisionAction.VISIBLE
    assert dm.visible_to == DecisionAction.AUDIT
    assert dm.replan_inject_to == DecisionAction.VISIBLE


def test_capabilities_rejects_missing_field() -> None:
    # Constructing Capabilities without all five fields must fail at schema time.
    with pytest.raises(Exception):
        Capabilities(block=True, visible=True, audit=True, mid_task_pull=True)  # type: ignore[call-arg]


# Boundary 2 enforcement -- adapter may NOT read fields outside the whitelist.
# Approach: pass an attribute-access-tracking proxy into the adapter method,
# then assert the recorded accesses are a subset of the documented allowed set.

_DECISION_ALLOWED = {"action", "severity", "message", "confidence"}
_DECISION_FORBIDDEN = {"evidence", "rule_id", "rule_version", "reasons"}
_BRIEF_ALLOWED = {"brief_text", "confidence", "focus_files", "cluster_files"}
_BRIEF_FORBIDDEN = {"candidates", "plan", "plan_path", "contracts", "constraints"}


class _AccessTracker:
    """__getattribute__ proxy that records every public-attribute access.

    Used to verify Boundary 2 contract: adapter MUST NOT read fields outside
    the documented per-type whitelist.
    """

    def __init__(self, target: Any, accessed: set[str]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_accessed", accessed)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        accessed = object.__getattribute__(self, "_accessed")
        accessed.add(name)
        return getattr(object.__getattribute__(self, "_target"), name)


def test_b2_adapter_only_reads_allowed_decision_fields() -> None:
    """Boundary 2: apply_decision must not touch evidence/rule_id/rule_version/reasons."""
    adapter = _FullCapAdapter()
    accessed: set[str] = set()
    decision = _make_decision(DecisionAction.BLOCK)
    proxy = _AccessTracker(decision, accessed)
    adapter.apply_decision(proxy)  # type: ignore[arg-type]
    forbidden_touched = accessed & _DECISION_FORBIDDEN
    assert not forbidden_touched, (
        f"adapter accessed forbidden Decision fields: {forbidden_touched}"
    )
    assert accessed.issubset(_DECISION_ALLOWED | {"action"}), (
        f"adapter read undocumented Decision fields: {accessed - _DECISION_ALLOWED}"
    )


def test_b2_adapter_only_reads_allowed_brief_fields() -> None:
    """Boundary 2: render_brief must not touch candidates/plan/plan_path."""
    from pathlib import Path

    adapter = _FullCapAdapter()
    accessed: set[str] = set()
    brief = BriefResult(
        brief_text="x",
        candidates=[],
        focus_files=[Path("src/a.py")],
        cluster_files=[Path("src/a.py")],
        confidence=0.7,
        plan={},
        plan_path=None,
    )
    proxy = _AccessTracker(brief, accessed)
    adapter.render_brief(proxy)  # type: ignore[arg-type]
    forbidden_touched = accessed & _BRIEF_FORBIDDEN
    assert not forbidden_touched, (
        f"adapter accessed forbidden BriefResult fields: {forbidden_touched}"
    )


def test_safe_render_strips_disallowed_paths() -> None:
    """Boundary 3: safe_render must drop file paths NOT in the allowed set."""
    from groundtruth.adapters.base import safe_render

    text = "Edit ranked targets first. See src/a.py and INTERNAL: src/secret/log.py."
    out = safe_render(text, {"src/a.py"})
    assert "src/a.py" in out
    assert "src/secret/log.py" not in out
