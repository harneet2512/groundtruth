"""Canonical kernel types.

All kernel functions accept and return these types. Adapters translate
scaffold-specific events into them and translate kernel decisions back.

See ``docs/kernel/API.md`` for field semantics and ``docs/kernel/telemetry.md``
for the on-disk schema of the ``KernelEvent`` records emitted via
``control.decision_log``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    """Base for value types that should not mutate after construction."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class ToolIntent(StrEnum):
    READ = "read"
    EDIT = "edit"
    SHELL = "shell"
    MCP_PULL = "mcp_pull"
    OTHER = "other"


class PullKind(StrEnum):
    TRACE = "trace"
    IMPACT = "impact"
    HOTSPOTS = "hotspots"
    VALIDATE = "validate"
    CONTEXT = "context"
    SYMBOLS = "symbols"


class DecisionAction(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    VISIBLE = "visible"
    AUDIT = "audit"


class ReplanStage(StrEnum):
    STAY_COURSE = "stay_course"
    CORRECTIVE = "corrective"
    RECOMPUTE = "recompute"


class ErrorClass(StrEnum):
    """Cursor-derived tool error taxonomy. ``Unknown`` = harness/adapter bug."""

    INVALID_ARGUMENTS = "InvalidArguments"
    UNEXPECTED_ENVIRONMENT = "UnexpectedEnvironment"
    PROVIDER_ERROR = "ProviderError"
    USER_ABORTED = "UserAborted"
    TIMEOUT = "Timeout"
    UNKNOWN = "Unknown"


class TaskInput(_Frozen):
    task_id: str
    repo_root: Path
    issue_text: str
    base_commit: str
    language_hint: str | None = None


class EditEvent(_Frozen):
    task_id: str
    files_changed: list[Path]
    diff_text: str | None = None
    diff_handle: str | None = None
    ts: str
    source_tool: str


class ToolCall(_Frozen):
    task_id: str
    tool_name: str
    args: dict[str, Any]
    ts: str
    intent: ToolIntent


class Capabilities(_Frozen):
    block: bool
    visible: bool
    audit: bool
    mid_task_pull: bool
    replan_inject: bool


class Candidate(_Frozen):
    path: Path
    score: float


class BriefResult(_Frozen):
    brief_text: str
    candidates: list[Candidate]
    focus_files: list[Path] = Field(default_factory=list, max_length=3)
    cluster_files: list[Path] = Field(default_factory=list)
    contracts: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    confidence: float
    plan: dict[str, Any]
    plan_path: Path | None = None


class RunState(BaseModel):
    """Kernel-owned per-task state. Mutates as the run progresses."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_id: str
    plan: dict[str, Any]
    brief_result: BriefResult | None = None
    edit_history: list[EditEvent] = Field(default_factory=list)
    viewed_files: list[Path] = Field(default_factory=list)
    warning_history: list[str] = Field(default_factory=list)
    capabilities: Capabilities
    model_hint: str | None = None


class PullQuery(_Frozen):
    kind: PullKind
    args: dict[str, Any]


class EditObservation(_Frozen):
    patch_shape: dict[str, Any]
    focus_hit_at_1: bool
    focus_hit_at_3: bool
    cluster_touch_rate: float
    root_scaffold_files_added: list[Path] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    expected_side_files_missing: list[Path] = Field(default_factory=list)


class Evidence(_Frozen):
    node_ids: list[int] = Field(default_factory=list)
    edge_ids: list[int] = Field(default_factory=list)
    candidates_top3: list[Candidate] = Field(default_factory=list)
    patch_shape: dict[str, Any] | None = None
    rule_inputs: dict[str, Any] = Field(default_factory=dict)


class Decision(_Frozen):
    action: DecisionAction
    severity: str
    reasons: list[str]
    message: str
    evidence: Evidence
    confidence: float
    rule_id: str
    rule_version: str


class PullResult(_Frozen):
    kind: PullKind
    payload: dict[str, Any]
    evidence: Evidence
    telemetry_record_id: str


class DriftSignals(_Frozen):
    first_edit_misses_focus: bool = False
    root_scaffold_added: bool = False
    graph_distance_growth: float = 0.0
    edits_outside_cluster_count: int = 0
    repeated_warnings: list[str] = Field(default_factory=list)


class ValidationResult(_Frozen):
    ok: bool
    broken_signatures: list[str] = Field(default_factory=list)
    orphaned_callers: list[str] = Field(default_factory=list)
    undefined_symbols: list[str] = Field(default_factory=list)
    evidence: Evidence


class ReplanTriggers(_Frozen):
    drift: DriftSignals
    validation: ValidationResult | None = None
    failing_tests_after_edit: bool = False


class Replan(_Frozen):
    stage: ReplanStage
    message: str
    next_actions: list[str] = Field(default_factory=list, max_length=3)
    new_candidates: list[Candidate] | None = None
    agent_focus_files: list[Path] = Field(default_factory=list, max_length=3)


class TriggeringState(_Frozen):
    scaffold: str
    event_kind: str
    tool: str | None = None
    edit_index: int | None = None


class ContextEvaluated(_Frozen):
    evidence: Evidence
    provenance: dict[str, Any]


class PolicyApplied(_Frozen):
    rule_id: str
    rule_version: str


class AuthorityExercised(_Frozen):
    adapter: str
    actual_action: DecisionAction
    degraded_from: DecisionAction | None = None


class KernelEvent(_Frozen):
    """Decision Trace 7-element record. See docs/adr/0003."""

    timestamp: str
    task_id: str
    triggering_state: TriggeringState
    context_evaluated: ContextEvaluated
    policy_applied: PolicyApplied
    alternatives_considered: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float
    action_selected: DecisionAction
    authority_exercised: AuthorityExercised


class Diff(_Frozen):
    """Opaque-ish diff handle used by ``validate_against_graph``."""

    diff_text: str
    files_changed: list[Path]


class GraphHandle(BaseModel):
    """Read-only port into the call graph. Real impl backed by ``graph.db``;
    test impl backed by inline JSON. Kept abstract here so unit tests can
    inject a mock without importing storage code.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str  # "sqlite" | "mock"
    graph_db_sha: str


__all__ = [
    "ToolIntent",
    "PullKind",
    "DecisionAction",
    "ReplanStage",
    "ErrorClass",
    "TaskInput",
    "EditEvent",
    "ToolCall",
    "Capabilities",
    "Candidate",
    "BriefResult",
    "RunState",
    "PullQuery",
    "EditObservation",
    "Evidence",
    "Decision",
    "PullResult",
    "DriftSignals",
    "ValidationResult",
    "ReplanTriggers",
    "Replan",
    "TriggeringState",
    "ContextEvaluated",
    "PolicyApplied",
    "AuthorityExercised",
    "KernelEvent",
    "Diff",
    "GraphHandle",
]
